"""HTTP interface: upload form, result pages, database admin, records view, JSON API."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

from .database import (
    MAX_LIST_LIMIT,
    MAX_SEARCH_CHARS,
    SEARCH_SCOPE_ALL,
    SEARCH_SCOPE_CONTENT,
    SEARCH_SCOPE_FILENAME,
    SEARCH_SCOPES,
    DatabaseManager,
    clamp_record_limit,
    clamp_record_page,
    clean_search_term,
    normalize_search_scope,
)
from .exceptions import (
    DatabaseError,
    DatabaseRecordNotFoundError,
    EmptyFileError,
    FileTooLargeError,
    MissingFileError,
    ResultExpiredError,
    UnsupportedFileTypeError,
)
from .excel import XLSX_EXTENSION, XLSX_MIMETYPE, records_workbook
from .ocr import ExtractionResult, extract_text
from .storage import ResultStore
from .utils import human_size, upload_limits

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


def _engine():
    return current_app.extensions["ocr_engine"]


def _store() -> ResultStore:
    return current_app.extensions["ocr_result_store"]


def _database() -> DatabaseManager:
    """The storage manager: one live store (MySQL or SQLite) plus the MySQL form data."""
    return current_app.extensions["ocr_database"]


# ---------------------------------------------------------------------------
# upload handling
# ---------------------------------------------------------------------------
def _read_upload() -> tuple[str, bytes]:
    """Validate the submitted multipart field and return ``(filename, bytes)``.

    The extension is only a first filter - :func:`app.ocr.documents.extract_text`
    still verifies the real file signature before anything is OCR'd.
    """
    storage = request.files.get("file")
    if storage is None or not (storage.filename or "").strip():
        raise MissingFileError("Choose a JPG, PNG or PDF file to upload.")

    filename = secure_filename(storage.filename) or "upload"
    suffix = Path(filename).suffix.lower()
    allowed = current_app.config["ALLOWED_EXTENSIONS"]
    if suffix not in allowed:
        raise UnsupportedFileTypeError(
            f"{suffix or 'That file type'} is not supported. "
            f"Allowed types: {current_app.config['ALLOWED_EXTENSIONS_LABEL']}."
        )

    data = storage.read()
    if not data:
        raise EmptyFileError(f"'{filename}' is empty.")

    limit = current_app.config.get("MAX_CONTENT_LENGTH") or 0
    if limit and len(data) > limit:
        raise FileTooLargeError(
            f"'{filename}' is {human_size(len(data))}; the limit is {human_size(limit)}."
        )
    return filename, data


@dataclass(frozen=True)
class ExtractionOutcome:
    """One completed extraction and the stored record it produced (if any).

    Persisting is deliberately non-fatal: an unreachable database must never cost
    the user the text they just waited for, so a failure is reported next to the
    result instead of raising.
    """

    result_id: str
    result: ExtractionResult
    record_id: int | None = None
    database_error: str | None = None


def _truthy(value: object, default: bool = False) -> bool:
    """Checkbox friendly boolean parsing (``1``/``on``/``true`` = yes)."""
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _checkbox_field(name: str, default: bool) -> bool:
    """Read a ``<hidden>`` + ``<checkbox>`` pair out of the submitted form.

    The upload and connect forms each submit the same field **twice**: a hidden ``0``
    (so an unticked box still sends something) and then the checkbox itself (``1``
    when ticked).  ``request.form.get()`` would return only the hidden ``0`` - which
    silently turned storing off, and stopped "Remember these details" from ever being
    written, however the box was set - so *any* truthy value wins and a request that
    sends no such field at all (the JSON API) keeps *default*.
    """
    values = request.form.getlist(name)
    if not values:
        return default
    return any(_truthy(value) for value in values)


def _remember_requested(payload) -> bool:
    """Whether this connect request wants the credentials written to disk.

    *payload* is the parsed JSON body (a plain mapping) or the submitted form (a
    :class:`~werkzeug.datastructures.MultiDict`, which carries the hidden+checkbox
    pair - see :func:`_checkbox_field`).  ``MYSQL_REMEMBER_SETTINGS`` is only the
    default for a request that does not mention it at all.
    """
    default = bool(current_app.config["MYSQL_REMEMBER_SETTINGS"])
    if hasattr(payload, "getlist"):
        return _checkbox_field("remember", default)
    return _truthy(payload.get("remember"), default)


def _should_save_to_database() -> bool:
    """Save when connected and the caller did not opt out for this upload."""
    manager = _database()
    if not manager.is_connected:
        return False
    config = current_app.config
    # ``MYSQL_AUTO_SAVE`` is the older name of the same flag.
    auto_save = bool(config.get("DATABASE_AUTO_SAVE", config.get("MYSQL_AUTO_SAVE", True)))
    return _checkbox_field("save_to_db", auto_save)


def _persist(result: ExtractionResult) -> tuple[int | None, str | None]:
    """Write *result* to the connected store, returning ``(record_id, error)``."""
    if not _should_save_to_database():
        return None, None
    database = _database()
    label = database.labels.label
    try:
        record_id = database.save_extraction(result)
    except DatabaseError as exc:
        logger.error("Could not save %r to %s: %s", result.filename, label, exc.message)
        return None, exc.message
    logger.debug("Saved %r as %s record %s", result.filename, label, record_id)
    return record_id, None


def _extract_and_store(filename: str, data: bytes) -> ExtractionOutcome:
    """Run the OCR pipeline, cache the result and optionally store it in the database."""
    config = current_app.config
    result = extract_text(
        data,
        filename,
        engine=_engine(),
        dpi=config["OCR_DPI"],
        max_pdf_pages=config["MAX_PDF_PAGES"],
        min_embedded_chars=config["MIN_EMBEDDED_TEXT_CHARS"],
        min_target_px=config["OCR_MIN_TARGET_PX"],
        max_upscale=config["OCR_MAX_UPSCALE"],
        max_render_pixels=config["OCR_MAX_RENDER_PIXELS"],
        preview_max_px=config["PREVIEW_MAX_PX"],
        max_image_pixels=config["MAX_IMAGE_PIXELS"],
    )
    result_id = _store().put(result)
    logger.debug("Stored extraction %s for %r", result_id, result.filename)
    record_id, database_error = _persist(result)
    return ExtractionOutcome(
        result_id=result_id,
        result=result,
        record_id=record_id,
        database_error=database_error,
    )


def result_payload(
    result: ExtractionResult,
    result_id: str,
    *,
    record_id: int | None = None,
    database_error: str | None = None,
) -> dict:
    """Serialise an :class:`~app.ocr.ExtractionResult` for the JSON API."""
    return {
        "ok": True,
        "result_id": result_id,
        "filename": result.filename,
        "kind": result.kind,
        "page_count": result.page_count,
        "char_count": result.char_count,
        "word_count": result.word_count,
        "confidence": result.confidence,
        "duration_ms": result.duration_ms,
        "engine": {
            "name": "tesseract",
            "version": result.tesseract_version,
            "languages": result.languages,
        },
        "text": result.full_text(),
        "pages": [
            {
                "page_number": page.page_number,
                "method": page.method,
                "char_count": page.char_count,
                "word_count": page.word_count,
                "confidence": page.confidence,
                "duration_ms": page.duration_ms,
                "text": page.text,
            }
            for page in result.pages
        ],
        "download_url": url_for("main.download_result", result_id=result_id),
        "database": {
            "connected": _database().is_connected,
            "saved": record_id is not None,
            "record_id": record_id,
            "error": database_error,
        },
    }


def _render_result(outcome: ExtractionOutcome, **extra):
    """Render the result page for an upload or a cached result."""
    return render_template(
        "result.html",
        result=outcome.result,
        result_id=outcome.result_id,
        database_record_id=outcome.record_id,
        database_error=outcome.database_error,
        limits=upload_limits(),
        **extra,
    )


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
@bp.get("/")
def index():
    """Upload form."""
    return render_template(
        "index.html", limits=upload_limits(), database=_database_status_payload()
    )


@bp.post("/upload")
def upload():
    """Accept a file, extract its text and render the result page."""
    filename, data = _read_upload()
    outcome = _extract_and_store(filename, data)
    return _render_result(outcome)


@bp.get("/result/<result_id>")
def show_result(result_id: str):
    """Re-open a stored extraction (results expire after ``RESULT_TTL_SECONDS``)."""
    result = _store().get(result_id)
    if result is None:
        raise ResultExpiredError(
            "That result is no longer available. Results are only kept for a short "
            "while - please upload the document again."
        )
    return _render_result(ExtractionOutcome(result_id=result_id, result=result))


@bp.get("/result/<result_id>/download")
def download_result(result_id: str):
    """Download the extracted text as a UTF-8 ``.txt`` file."""
    result = _store().get(result_id)
    if result is None:
        raise ResultExpiredError(
            "That result has expired and can no longer be downloaded. "
            "Please upload the document again."
        )
    stem = Path(result.filename).stem or "extraction"
    buffer = io.BytesIO(result.full_text().encode("utf-8"))
    return send_file(
        buffer,
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=f"{stem}.ocr.txt",
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@bp.get("/api/health")
def api_health():
    """Report whether the OCR engine is usable (handy for deployment checks)."""
    health = _engine().health()
    database = _database_status_payload()
    return (
        jsonify(
            {
                "ok": bool(health["available"]),
                "engine": health,
                "limits": {
                    "max_upload": human_size(current_app.config.get("MAX_CONTENT_LENGTH")),
                    "allowed_extensions": sorted(current_app.config["ALLOWED_EXTENSIONS"]),
                    "max_pdf_pages": current_app.config["MAX_PDF_PAGES"],
                    "min_embedded_text_chars": current_app.config["MIN_EMBEDDED_TEXT_CHARS"],
                },
                "database": {
                    "provider": database["provider"],
                    "backend": database["backend"],
                    "connected": database["connected"],
                    "driver_available": database["driver"]["available"],
                    "sqlite_available": database["backends"]["sqlite"]["available"],
                    "connection_label": database["connection_label"],
                    "saved_settings": database["saved_settings"],
                    "record_count": database.get("record_count"),
                },
            }
        ),
        200 if health["available"] else 503,
    )


@bp.post("/api/ocr")
def api_ocr():
    """Same pipeline as ``/upload`` but returns JSON - script/automation friendly."""
    filename, data = _read_upload()
    outcome = _extract_and_store(filename, data)
    return (
        jsonify(
            result_payload(
                outcome.result,
                outcome.result_id,
                record_id=outcome.record_id,
                database_error=outcome.database_error,
            )
        ),
        200,
    )


# ---------------------------------------------------------------------------
# storage admin (UI + API)
# ---------------------------------------------------------------------------
#: Fields echoed back into the form after a failed connect (never the password).
#: ``path`` is the SQLite field - the same echo helps whoever typed that one.
_FORM_FIELDS = (
    "host",
    "port",
    "user",
    "database",
    "table",
    "pages_table",
    "charset",
    "connect_timeout",
    "path",
)


def _database_status_payload() -> dict:
    """Connection status, plus the row count when the server answers."""
    manager = _database()
    payload = manager.status()
    payload["record_count"] = None
    if manager.is_connected:
        try:
            payload["record_count"] = manager.record_count()
        except DatabaseError as exc:
            payload["last_error"] = exc.message
    return payload


def _schema_message(status: dict) -> str:
    """Plain language summary of what connecting actually did.

    Reads the status payload, so the same wording is used for the page, the JSON API
    and both backends - the store the caller connected decides the nouns.
    """
    settings = status.get("settings") or {}
    sqlite = status.get("provider") == "sqlite"
    label = "SQLite" if sqlite else "MySQL"
    parts = [
        f"Connected to {status.get('connection_label') or settings.get('label')}"
        f" ({label} {status.get('server_version') or 'unknown'})."
    ]
    if sqlite:
        if status.get("database_created"):
            parts.append(f"Created the SQLite file '{settings.get('path')}'.")
        if status.get("tables_created"):
            parts.append("Created the missing tables.")
        if not status.get("database_created") and not status.get("tables_created"):
            parts.append("The file and its tables were already in place.")
        parts.append("Extracted text is stored in this file from now on.")
        return " ".join(parts)

    if status.get("database_created"):
        parts.append(f"Created the schema '{settings.get('database')}'.")
    if status.get("tables_created"):
        parts.append("Created the missing tables.")
    if not status.get("database_created") and not status.get("tables_created"):
        parts.append("Schema and tables were already in place.")
    parts.append("Extracted text is stored in these tables from now on.")
    return " ".join(parts)


def _form_values(form, **overrides) -> dict:
    """Echo back what the user typed, so a failed connect keeps the form filled in."""
    values = {key: form.get(key, "") for key in _FORM_FIELDS}
    values["password"] = ""
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# records view (browse + search)
# ---------------------------------------------------------------------------
#: Labels for the "search in" selector - the keys are the storage layer scopes.
_SEARCH_SCOPE_LABELS = {
    SEARCH_SCOPE_ALL: "File name and text",
    SEARCH_SCOPE_FILENAME: "File name only",
    SEARCH_SCOPE_CONTENT: "Extracted text only",
}

#: The ``?limit=`` value (and the toolbar option) that asks for **every** stored
#: record instead of one page of them - the records view's default.  It maps to
#: :data:`MAX_LIST_LIMIT`, the widest page the storage layer will bind, so a store
#: bigger than that still pages instead of dragging an unbounded result set in.
ROWS_ALL = "all"
#: Page sizes offered next to "All"; whichever one is in use is offered too, so the
#: selector never lies about what is on screen.  "All" is the cap itself
#: (:data:`MAX_LIST_LIMIT`), which is why no preset repeats it.
ROW_CHOICES = (10, 25, 50, 100)


def _default_page_size() -> int:
    """``MYSQL_RECORDS_LIMIT``, never wider than the storage layer's cap.

    The shipped default *is* the cap (= :data:`ROWS_ALL` rows), which is what makes
    a store that is not empty list all of its records on one page out of the box.
    """
    return clamp_record_limit(current_app.config["MYSQL_RECORDS_LIMIT"], MAX_LIST_LIMIT)


def _record_limit(value: object) -> int:
    """The page size of this request - the whole store unless asked otherwise.

    Needing no ``?limit=`` at all is deliberate: a blank or unusable value keeps the
    configured default (:func:`_default_page_size`) and ``?limit=all`` asks for the
    whole store explicitly, so the records view shows everything the database holds
    rather than stopping after the first page of it.  A real number still narrows the
    page and is clamped exactly as before.
    """
    text = "" if value is None else str(value).strip()
    if text.lower() == ROWS_ALL:
        return MAX_LIST_LIMIT
    return clamp_record_limit(text, _default_page_size())


def _rows_per_page_choice(limit: int) -> str:
    """Which **Rows per page** option matches *limit* (``all`` for the widest page)."""
    return ROWS_ALL if limit >= MAX_LIST_LIMIT else str(limit)


def _rows_per_page_choices(limit: int) -> list[tuple[str, str]]:
    """The **Rows per page** options: "All", the usual page sizes, and *limit*.

    Whatever is in use is always among the options - even a hand written ``?limit=7``
    that no preset covers - so the selector keeps showing the page size that produced
    the table underneath it.
    """
    sizes = sorted({size for size in (*ROW_CHOICES, limit) if size < MAX_LIST_LIMIT})
    choices = [(ROWS_ALL, f"All (up to {MAX_LIST_LIMIT})")]
    choices.extend((str(size), f"{size} rows") for size in sizes)
    return choices


def _record_filters(source=None) -> dict:
    """Validated ``q``/``scope``/``limit``/``page`` from the query string (or a form).

    Every value is clamped to something the storage layer accepts, so a hand
    written URL cannot turn into a full table scan or an over-long ``LIKE``.
    ``limit`` is the **page size** - every stored record by default - and ``page``
    the 1-based page of the listing.
    """
    source = request.args if source is None else source
    return {
        "q": clean_search_term(source.get("q")),
        "scope": normalize_search_scope(source.get("scope")),
        "limit": _record_limit(source.get("limit")),
        "page": clamp_record_page(source.get("page")),
    }


def _record_query(filters: dict, *, page: int | None = None) -> dict:
    """The query string of one records view request: the filters, minus a blank term.

    *page* defaults to the page being viewed.  Page 1 is left out on purpose, so the
    plain list (and every "back to the first page" link) keeps the short URL it
    always had.  ``limit`` is written exactly as the toolbar writes it, so "All" stays
    recognisable in a link instead of turning into a bare number.
    """
    target = filters.get("page", 1) if page is None else page
    query = {"scope": filters["scope"], "limit": _rows_per_page_choice(filters["limit"])}
    if filters["q"]:
        query["q"] = filters["q"]
    if target > 1:
        query["page"] = target
    return query


def _page_url(endpoint: str, filters: dict, page: int) -> str:
    """A link to one page of the records table, with the current filters applied."""
    return url_for(endpoint, **_record_query(filters, page=page))


def _page_links(
    page: int, pages: int, endpoint: str, filters: dict, *, window: int = 2
) -> list[dict]:
    """The page buttons: a window around *page* plus the first and the last page.

    A ``None`` page means "there is a gap here" - the template prints an ellipsis
    instead of a button, so a 400 page result does not produce 400 links.
    """
    wanted = sorted(
        number
        for number in {1, pages, *range(page - window, page + window + 1)}
        if 1 <= number <= pages
    )
    links: list[dict] = []
    previous = 0
    for number in wanted:
        if number - previous > 1:
            links.append({"page": None, "url": None, "current": False})
        links.append(
            {
                "page": number,
                "url": _page_url(endpoint, filters, number),
                "current": number == page,
            }
        )
        previous = number
    return links


def _page_count(total: int, limit: int) -> int:
    """How many pages *limit* rows per page need - at least one, even when empty."""
    return max(1, -(-total // limit))  # ceil, without floating point


def _records_view(*, endpoint: str = "main.database_page", error: str | None = None) -> dict:
    """Everything the records table needs: one page of rows, its filters, a summary.

    The listing is **paged**: ``limit`` rows per page and ``page`` to walk the rest.
    ``limit`` defaults to :data:`ROWS_ALL` (the storage layer's :data:`MAX_LIST_LIMIT`
    cap), so a store that is not empty lists *every* stored record on one page and a
    bigger one pages at that cap.  The total comes from the same search criteria
    (``COUNT(*)``), which is what lets the page buttons know how many pages there are
    - and a ``?page=`` past the end lands on the last page instead of an empty table.

    Reading is best-effort by design: without a live connection (or when the last
    statement failed) the table still renders and ``error`` says why.
    """
    manager = _database()
    filters = _record_filters()
    view = {
        **filters,
        "action": endpoint,
        "export_url": url_for("main.database_records_export", **_record_query(filters)),
        "scope_label": _SEARCH_SCOPE_LABELS[filters["scope"]],
        "scopes": [(scope, _SEARCH_SCOPE_LABELS[scope]) for scope in SEARCH_SCOPES],
        "max_limit": MAX_LIST_LIMIT,
        "max_search_chars": MAX_SEARCH_CHARS,
        "limit_choice": _rows_per_page_choice(filters["limit"]),
        "limit_choices": _rows_per_page_choices(filters["limit"]),
        "showing_all": filters["limit"] >= MAX_LIST_LIMIT,
        "records": [],
        "count": 0,
        "total": 0,
        "pages": 1,
        "page_links": [],
        "prev_url": None,
        "next_url": None,
        "range_label": "",
        "searching": bool(filters["q"]),
        "error": error,
    }
    if error is not None or not manager.is_connected:
        return view
    try:
        total = manager.count_extractions(filters["q"], scope=filters["scope"])
        pages = _page_count(total, filters["limit"])
        page = min(filters["page"], pages)
        offset = (page - 1) * filters["limit"]
        records = manager.search_extractions(
            filters["q"], filters["limit"], scope=filters["scope"], offset=offset
        )
    except DatabaseError as exc:
        view["error"] = exc.message
        return view

    filters = {**filters, "page": page}
    view.update(
        filters,
        records=records,
        count=len(records),
        total=total,
        pages=pages,
        page_links=_page_links(page, pages, endpoint, filters),
        prev_url=_page_url(endpoint, filters, page - 1) if page > 1 else None,
        next_url=_page_url(endpoint, filters, page + 1) if page < pages else None,
        range_label=f"{offset + 1}-{offset + len(records)}" if records else "",
        # The export follows the page that is actually shown, not the one asked for.
        export_url=url_for("main.database_records_export", **_record_query(filters)),
    )
    return view


# ---------------------------------------------------------------------------
# Excel export of the stored records
# ---------------------------------------------------------------------------
def _xlsx_filename(stem: str) -> str:
    """``records-invoice-20260929-153012.xlsx`` - safe for every browser and OS."""
    safe = secure_filename(stem) or "records"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{safe[:48]}-{stamp}{XLSX_EXTENSION}"


def _workbook_response(payload: bytes, filename: str):
    """Serve generated workbook bytes as an in-memory download."""
    return send_file(
        io.BytesIO(payload),
        mimetype=XLSX_MIMETYPE,
        as_attachment=True,
        download_name=filename,
    )


def _row_limit_label(limit: int) -> str:
    """How the ``Export`` sheet describes the page size of the rows it holds."""
    if limit >= MAX_LIST_LIMIT:
        return f"all stored records (max {MAX_LIST_LIMIT})"
    return str(limit)


def _export_facts(
    status: dict,
    *,
    rows: int,
    filters: dict | None = None,
    record: dict | None = None,
) -> list[tuple[str, object]]:
    """The ``Export`` sheet: what this file is, where it came from, what it holds."""
    settings = status.get("settings") or {}
    facts: list[tuple[str, object]] = [
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("Server", status.get("connection_label") or settings.get("label") or "unknown"),
        ("Schema", settings.get("database")),
        ("Table", settings.get("table")),
        ("Pages table", settings.get("pages_table")),
    ]
    if record is not None:
        facts.append(
            ("Record", f"#{record.get('id')} - {record.get('filename') or 'unnamed'}")
        )
    if filters is not None:
        facts.extend(
            [
                ("Search term", filters["q"] or "(none - newest records)"),
                ("Search scope", _SEARCH_SCOPE_LABELS[filters["scope"]]),
                ("Row limit", _row_limit_label(filters["limit"])),
                ("Page", filters.get("page", 1)),
            ]
        )
    facts.append(("Rows exported", rows))
    return facts


def _local_path(value: object) -> str | None:
    """A same-site path from a form field (``None`` for anything else).

    Only relative paths are honoured, so a crafted ``next`` cannot bounce the user
    to another host.
    """
    target = "" if value is None else str(value).strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    if any(char in target for char in ("\r", "\n", "\\")):
        return None
    return target


def _render_database_page(*, error=None, message=None, form=None, status=200):
    """Connection form + live status + the records table."""
    manager = _database()
    view = _records_view(error=error)
    html = render_template(
        "database.html",
        database=_database_status_payload(),
        records_view=view,
        error=error,
        message=message,
        form={**manager.form_defaults(), **(form or {})},
        limits=upload_limits(),
    )
    return html, status


@bp.get("/database")
def database_page():
    """Connect a store (MySQL server or local SQLite file) and browse what was stored."""
    return _render_database_page()


@bp.post("/database/connect")
def database_connect():
    """Connect the submitted store (MySQL or SQLite) and remember MySQL credentials.

    The form carries a ``backend`` field (``mysql``/``sqlite``); for MySQL the schema
    is created on the way in, for SQLite the file, its tables and indexes.
    """
    manager = _database()
    form = request.form.to_dict()
    remember = _remember_requested(request.form)
    try:
        manager.connect(request.form, remember=remember)
    except DatabaseError as exc:
        logger.error("Database connect failed: %s", exc.message)
        return _render_database_page(
            error=exc.message, form=_form_values(form), status=exc.status_code
        )
    return _render_database_page(message=_schema_message(_database_status_payload()))


@bp.post("/database/disconnect")
def database_disconnect():
    """Close the connection (remembered credentials stay put)."""
    _database().disconnect()
    return redirect(url_for("main.database_page"), code=303)


@bp.post("/database/schema")
def database_ensure_schema():
    """Explicitly re-run the table creation for the live connection."""
    try:
        created = _database().ensure_schema()
    except DatabaseError as exc:
        return _render_database_page(error=exc.message, status=exc.status_code)
    message = (
        "Created the missing tables."
        if created
        else "The schema and both tables already exist - nothing to create."
    )
    return _render_database_page(message=message)


@bp.post("/database/forget")
def database_forget():
    """Delete the remembered credentials (the live connection is kept)."""
    forgotten = _database().forget_settings()
    message = (
        "The saved connection details were deleted."
        if forgotten
        else "There were no saved connection details to delete."
    )
    return _render_database_page(message=message)


@bp.get("/database/records")
def database_records_page():
    """Records view: every stored record, searchable and browsable (one page)."""
    return render_template(
        "records.html",
        database=_database_status_payload(),
        records_view=_records_view(endpoint="main.database_records_page"),
        limits=upload_limits(),
    )


@bp.get("/database/records/export.xlsx")
def database_records_export():
    """Download the rows the records table is showing as an Excel workbook.

    The current search, scope, row limit **and page** are applied, and the page is
    clamped exactly like the view clamps it, so the **Export .xlsx** button next to
    the search box always saves what is on screen (by default: every stored record) -
    the metadata, the full extracted text of every record and one row per stored page.
    The workbook is built in memory - nothing is written to disk.
    """
    manager = _database()
    filters = _record_filters()
    total = manager.count_extractions(filters["q"], scope=filters["scope"])
    total_pages = _page_count(total, filters["limit"])
    page = min(filters["page"], total_pages)
    offset = (page - 1) * filters["limit"]
    records = manager.export_extractions(
        filters["q"], filters["limit"], scope=filters["scope"], offset=offset
    )
    filters = {**filters, "page": page}
    # The pages query is one round trip for every exported record; the file name of
    # each record is already in hand, so the Pages sheet is readable on its own.
    names = {record["id"]: record.get("filename") for record in records}
    pages = [
        {**row, "filename": names.get(row.get("extraction_id"))}
        for row in manager.pages_for_extractions(list(names))
    ]
    payload = records_workbook(
        records,
        pages=pages,
        facts=_export_facts(manager.status(), rows=len(records), filters=filters),
        title=f"Stored OCR records ({len(records)})",
    )
    filename = _xlsx_filename(f"records {filters['q']}")
    logger.info(
        "Exported %s stored record(s) from page %s/%s to %s",
        len(records),
        page,
        total_pages,
        filename,
    )
    return _workbook_response(payload, filename)


@bp.get("/database/records/<int:record_id>")
def database_record(record_id: int):
    """One stored extraction, with its per-page text."""
    record = _database().require_extraction(record_id)
    return render_template(
        "record.html", record=record, database=_database().status(), limits=upload_limits()
    )


@bp.get("/database/records/<int:record_id>/download")
def database_record_download(record_id: int):
    """Download a stored extraction as a UTF-8 ``.txt`` file."""
    record = _database().require_extraction(record_id)
    stem = Path(str(record.get("filename") or "extraction")).stem or "extraction"
    buffer = io.BytesIO(str(record.get("content") or "").encode("utf-8"))
    return send_file(
        buffer,
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=f"{stem}.db.txt",
    )


@bp.get("/database/records/<int:record_id>/export.xlsx")
def database_record_export(record_id: int):
    """Download one stored extraction (metadata, text and its pages) as a workbook."""
    manager = _database()
    record = manager.require_extraction(record_id)
    pages = [
        {**page, "extraction_id": record.get("id"), "filename": record.get("filename")}
        for page in record.get("pages") or []
    ]
    payload = records_workbook(
        [record],
        pages=pages,
        facts=_export_facts(manager.status(), rows=1, record=record),
        title=f"Stored OCR record #{record_id}",
    )
    stem = Path(str(record.get("filename") or "extraction")).stem
    filename = _xlsx_filename(f"{stem} record {record_id}")
    logger.info("Exported stored record %s to %s", record_id, filename)
    return _workbook_response(payload, filename)


@bp.post("/database/records/<int:record_id>/delete")
def database_record_delete(record_id: int):
    """Delete one stored extraction (its page rows cascade)."""
    manager = _database()
    manager.require_extraction(record_id)  # 404 when it is already gone
    manager.delete_extraction(record_id)
    logger.info("Deleted stored record %s", record_id)
    # Back to the list the button was on, with its search still applied.
    target = _local_path(request.form.get("next")) or url_for("main.database_page")
    return redirect(target, code=303)


# ---------------------------------------------------------------------------
# JSON API - storage admin
# ---------------------------------------------------------------------------
@bp.get("/api/database")
def api_database():
    """Connection status, driver version and the stored row count."""
    return jsonify({"ok": True, "database": _database_status_payload()})


@bp.post("/api/database/connect")
def api_database_connect():
    """Connect a store: MySQL (``host``/``user``/...) or SQLite (``path``).

    ``{"backend": "sqlite"}`` - or a ``path`` - picks the local file; anything else
    asks MySQL.  Both create what they need (the schema and tables, or the file, its
    tables and its indexes), so calling this twice is safe.
    """
    payload = request.get_json(silent=True) or request.form
    _database().connect(payload, remember=_remember_requested(payload))
    status_payload = _database_status_payload()
    status_payload["message"] = _schema_message(status_payload)
    return jsonify({"ok": True, "database": status_payload})


@bp.post("/api/database/disconnect")
def api_database_disconnect():
    """Close the connection; the remembered credentials stay on disk."""
    manager = _database()
    was_connected = manager.is_connected
    manager.disconnect()
    return jsonify(
        {"ok": True, "was_connected": was_connected, "database": _database_status_payload()}
    )


@bp.get("/api/database/records")
def api_database_records():
    """Newest first; ``?q=`` searches, ``?scope=`` narrows it, ``?limit=``/``?page=`` page.

    Without ``?limit=`` the whole store is returned, up to the 200 row cap
    (:data:`MAX_LIST_LIMIT`) - the same default the records view shows; ``?limit=all``
    asks for that explicitly.
    """
    manager = _database()
    filters = _record_filters()
    total = manager.count_extractions(filters["q"], scope=filters["scope"])
    pages = _page_count(total, filters["limit"])
    page = min(filters["page"], pages)
    records = manager.search_extractions(
        filters["q"],
        filters["limit"],
        scope=filters["scope"],
        offset=(page - 1) * filters["limit"],
    )
    return jsonify(
        {
            "ok": True,
            "database": _database_status_payload(),
            "query": {
                **filters,
                "page": page,
                "count": len(records),
                "total": total,
                "pages": pages,
            },
            "records": records,
        }
    )


@bp.get("/api/database/records/<int:record_id>")
def api_database_record(record_id: int):
    """One record including the full text and its page rows."""
    return jsonify({"ok": True, "record": _database().require_extraction(record_id)})


@bp.delete("/api/database/records/<int:record_id>")
def api_database_record_delete(record_id: int):
    database = _database()
    if not database.delete_extraction(record_id):
        raise DatabaseRecordNotFoundError(
            f"Record #{record_id} does not exist in the connected database."
        )
    return jsonify({"ok": True, "deleted": record_id})


