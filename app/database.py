"""Optional relational sink for extracted text - MySQL, with SQLite as the fallback.

The OCR pipeline itself stays stateless: uploads are processed in memory and the
result cache in :mod:`app.storage` is only a short lived convenience.  This
module adds a *relational* home for the extracted data so results survive a
restart and can be queried with plain SQL:

``ocr_extractions``
    One row per processed upload - at minimum the **file name**, the **upload
    date/time** (UTC) and the **extracted content**, plus the statistics the
    result page already shows (pages, characters, words, confidence, engine).

``ocr_extractions_pages``
    One row per page, so a multi-page PDF can be queried page by page (the name is
    the parent table plus ``_pages``, see :data:`PAGES_TABLE_SUFFIX`).  The
    foreign key cascades, deleting an extraction removes its pages.

Nothing is created or written until an operator connects a server from the
``/database`` page (or the JSON API / ``DATABASE_AUTO_CONNECT``).  Connecting is
also where the schema comes from: the database is created when missing
(``CREATE DATABASE IF NOT EXISTS``) and the tables are created or upgraded with
``CREATE TABLE IF NOT EXISTS``.

Reading is served from here too: ``recent_extractions`` lists the newest rows for
the records view, and ``search_extractions`` finds a record by file name, by a
snippet of the stored text or by its ``id``.  Search terms are bound as ``LIKE``
patterns whose wildcards are escaped, so ``%`` and ``_`` typed by a user match
literally instead of widening the search.  ``count_extractions`` counts what those
same criteria match, and the listing queries take an ``offset`` next to their
``limit`` - together they are what makes the records view **paged**: one
``LIMIT``/``OFFSET`` query per page, and a page count to draw buttons for.
``export_extractions`` runs that same
search but selects the whole ``content`` column - the Excel export
(:mod:`app.excel`) wants documents, not snippets - and ``pages_for_extractions``
returns the page rows of every exported record in a single ``IN (...)`` query.

The driver is PyMySQL - pure Python, so ``pip install`` needs no compiler.  It is
imported defensively: the application still starts (and reports the problem on
the ``/database`` page) when the package is missing.

The *same* store is available without any server at all: :mod:`app.sqlite` builds
these two tables in one file with the standard library, which is what makes the
whole storage path testable (and usable) on a machine that has no MySQL.  Both
stores implement the same surface, so everything below - the settings validation,
the identifiers, the DDL builders, the search clauses and the row serialisation -
is shared, and :class:`DatabaseManager` is the one place that picks between them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .exceptions import (
    DatabaseError,
    DatabaseNotConfiguredError,
    DatabaseRecordNotFoundError,
    DatabaseUnavailableError,
    DatabaseWriteError,
    InvalidDatabaseSettingsError,
)
from .fields import FIELD_ORDER, DocumentFields
from .ocr import ExtractionResult


try:  # pragma: no cover - exercised through the "driver missing" code paths
    import pymysql
    import pymysql.cursors
    import pymysql.err
except ImportError:  # pragma: no cover - only on a broken installation
    pymysql = None

logger = logging.getLogger(__name__)

PROVIDER = "mysql"
DRIVER_NAME = "PyMySQL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3306
DEFAULT_USER = "root"
DEFAULT_DATABASE = "flask_ocr"
DEFAULT_TABLE = "ocr_extractions"
DEFAULT_CHARSET = "utf8mb4"
DEFAULT_CONNECT_TIMEOUT = 8
PAGES_TABLE_SUFFIX = "_pages"

#: Rows returned by ``recent_extractions`` when no (or a silly) limit is given.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200
#: Highest page the records view will hop to.  The view clamps ``?page=`` to the
#: real page count before it queries anything, so this only stops a hand written
#: URL from asking a database to walk an absurd offset.
MAX_RECORD_PAGE = 10_000
#: Characters of extracted content shown in the record list.
RECORD_PREVIEW_CHARS = 140
#: ``filename`` is ``VARCHAR(255)``; MySQL would truncate the rest anyway.
MAX_FILENAME_CHARS = 255

#: Where a search looks: the file name, the extracted text, or both.
SEARCH_SCOPE_ALL = "all"
SEARCH_SCOPE_FILENAME = "filename"
SEARCH_SCOPE_CONTENT = "content"
SEARCH_SCOPES = (SEARCH_SCOPE_ALL, SEARCH_SCOPE_FILENAME, SEARCH_SCOPE_CONTENT)
DEFAULT_SEARCH_SCOPE = SEARCH_SCOPE_ALL
#: Longer search terms are truncated - nobody scans for a 4 KB pattern on purpose.
MAX_SEARCH_CHARS = 120
#: Escape character for ``LIKE``: ``%``/``_`` typed by a user must match literally.
#: Deliberately ``!`` and not a backslash, so the pattern cannot depend on
#: ``sql_mode`` (``NO_BACKSLASH_ESCAPES``) or on the driver's escaping.
LIKE_ESCAPE = "!"
#: Highest ``BIGINT UNSIGNED`` - a numeric term beyond it cannot be a record id.
MAX_RECORD_ID = 2**64 - 1

#: Identifier whitelist - the only source for table/database names, because
#: MySQL cannot bind identifiers as parameters.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
#: MySQL refuses identifiers longer than this (error 1059).
MAX_IDENTIFIER_CHARS = 64

_INSTALL_HINT = (
    "Install the MySQL driver with: "
    "env\\Scripts\\python.exe -m pip install -r requirements.txt"
)

#: Backend ids: ``DATABASE_BACKEND``, the connect form and the JSON API all pick a
#: store with one of these.  ``auto`` means "MySQL when it is available, the local
#: SQLite file when it is not" - see :mod:`app.sqlite`.
BACKEND_AUTO = "auto"
BACKEND_MYSQL = "mysql"
BACKEND_SQLITE = "sqlite"
BACKENDS = (BACKEND_MYSQL, BACKEND_SQLITE)
DEFAULT_BACKEND = BACKEND_AUTO


@dataclass(frozen=True)
class Backend:
    """How one store is named in the UI, the API and the log lines.

    Templates read these through ``store`` (see :func:`app.create_app`), so no page
    has to say "MySQL" while the SQLite file is the connected store.
    """

    id: str
    #: "MySQL" / "SQLite" - used as a label, e.g. "MySQL storage".
    label: str
    #: "MySQL server" / "SQLite database" - finishes "No ... is connected".
    server_term: str
    #: "a MySQL server" / "a SQLite database" - finishes "Connect to ...".
    server_phrase: str


STORES: dict[str, Backend] = {
    BACKEND_MYSQL: Backend(BACKEND_MYSQL, "MySQL", "MySQL server", "a MySQL server"),
    BACKEND_SQLITE: Backend(
        BACKEND_SQLITE, "SQLite", "SQLite database", "a SQLite database"
    ),
}


def normalize_backend(value: object, default: str = DEFAULT_BACKEND) -> str:
    """A known backend id, or *default* for anything else.

    Unknown values fall back instead of raising: a stale bookmark must not break the
    page, and ``auto`` is the safe answer (it tries MySQL first).
    """
    candidate = "" if value is None else str(value).strip().lower()
    if candidate in BACKENDS or candidate == BACKEND_AUTO:
        return candidate
    return default


def backend_labels(backend: object) -> Backend:
    """The names for *backend*, with ``auto`` presented as MySQL.

    ``auto`` asks MySQL first, so that is what the UI offers until a connection
    proves otherwise.
    """
    return STORES.get(normalize_backend(backend, DEFAULT_BACKEND), STORES[BACKEND_MYSQL])

EXTRACTIONS_COLUMNS = (
    "filename",
    "uploaded_at",
    "content",
    "kind",
    "page_count",
    "char_count",
    "word_count",
    "confidence",
    "duration_ms",
    "size_bytes",
    "ocr_language",
    "engine_version",
    # The structured fields (app/fields.py) - reviewed values, in FIELD_ORDER, so
    # ``save_extraction`` can bind ``DocumentFields.to_row()`` positionally.
    *FIELD_ORDER,
    "content_sha256",
)

#: Column type of every structured field, per dialect.  ``app.sqlite`` uses the
#: same keys with its own types; both dialects add these columns to a table that
#: was created before the fields existed (see ``_add_field_columns``).
MYSQL_FIELD_COLUMN_TYPES: dict[str, str] = {
    "supplier": "VARCHAR(255)",
    "invoice_number": "VARCHAR(64)",
    "document_date": "DATE",
    "total_amount": "DECIMAL(12,2)",
    "currency": "CHAR(3)",
}

#: The ``SELECT`` list of the structured fields, in ``FIELD_ORDER``.
FIELD_COLUMNS = ", ".join(f"`{name}`" for name in FIELD_ORDER)


PAGES_COLUMNS = (
    "page_number",
    "method",
    "content",
    "char_count",
    "word_count",
    "confidence",
    "duration_ms",
)

#: What the record list selects - never the whole document, only a preview.
#: ``%s`` is the preview length, which is why it is bound rather than inlined.
LIST_COLUMNS = (
    "`id`, `filename`, `uploaded_at`, `kind`, `page_count`, `char_count`, "
    "`word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, "
    "`engine_version`, `stored_at`, "
    f"{FIELD_COLUMNS}, "
    "LEFT(`content`, %s) AS `preview`, LENGTH(`content`) AS `content_chars`"
)

#: What the Excel export selects - the same rows, but with the *full* text.
EXPORT_COLUMNS = (
    "`id`, `filename`, `uploaded_at`, `kind`, `page_count`, `char_count`, "
    "`word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, "
    f"`engine_version`, {FIELD_COLUMNS}, `stored_at`, `content_sha256`, `content`"
)


#: Columns of one pages row, for the export sheet (one row per page).
EXPORT_PAGE_COLUMNS = (
    "`extraction_id`, `page_number`, `method`, `char_count`, `word_count`, "
    "`confidence`, `duration_ms`, `content`"
)

#: Columns each search scope looks at (a numeric term also matches ``id``).
_SEARCH_COLUMNS: dict[str, tuple[str, ...]] = {
    SEARCH_SCOPE_ALL: ("filename", "content"),
    SEARCH_SCOPE_FILENAME: ("filename",),
    SEARCH_SCOPE_CONTENT: ("content",),
}


# ---------------------------------------------------------------------------
# driver helpers
# ---------------------------------------------------------------------------
def driver_available() -> bool:
    """True when PyMySQL could be imported."""
    return pymysql is not None


def driver_version() -> str | None:
    """Version string of the installed driver (``None`` when missing)."""
    if pymysql is None:
        return None
    for attribute in ("__version__", "VERSION", "version_info"):
        value = getattr(pymysql, attribute, None)
        if value:
            return str(value)
    return None  # pragma: no cover - every release exposes one of the three


def _require_driver() -> None:
    if pymysql is None:  # pragma: no cover - depends on the installation
        raise DatabaseUnavailableError(
            f"The MySQL driver ({DRIVER_NAME}) is not installed. {_INSTALL_HINT}"
        )


def _mysql_error_types() -> tuple[type[BaseException], ...]:
    """Exception classes that mean "the server or the statement failed"."""
    if pymysql is None:  # pragma: no cover - defensive
        return (OSError,)
    return (pymysql.err.MySQLError, OSError)


def _sqlite_module():
    """Import :mod:`app.sqlite` lazily.

    The SQLite store builds on the helpers in this module (search clauses, column
    lists, row serialisation), so importing it at the top would be circular.
    """
    from . import sqlite as module

    return module


def _sqlite_available() -> bool:
    """True when the standard library ``sqlite3`` module is present.

    It is part of every normal CPython build, so this only guards the fallback on a
    Python compiled without it.
    """
    try:
        _sqlite_module()
    except ImportError:  # pragma: no cover - a Python built without sqlite3
        return False
    return True


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------
def sanitize_identifier(
    value: object, default: str, *, label: str, dialect: str = "MySQL"
) -> str:
    """Return a safe identifier, falling back to *default* when empty.

    Names can never be passed as bound parameters, so they are whitelisted
    instead of escaped: 1-64 characters of letters, digits and underscores.
    """
    candidate = "" if value is None else str(value).strip().strip("`")
    if not candidate:
        candidate = default
    if not _IDENTIFIER_PATTERN.match(candidate):
        raise InvalidDatabaseSettingsError(
            f"{label} {value!r} is not a valid {dialect} name. Use 1-64 characters: "
            "letters, digits or underscores."
        )
    return candidate


def quote_identifier(name: str, *, label: str = "Identifier", dialect: str = "MySQL") -> str:
    """Backtick-quote an identifier after validating it.

    Both MySQL and SQLite accept backticks, so the one helper serves both stores.
    """
    if not _IDENTIFIER_PATTERN.match(name or ""):
        raise InvalidDatabaseSettingsError(
            f"{label} {name!r} is not a valid {dialect} name. Use 1-64 characters: "
            "letters, digits or underscores."
        )
    return f"`{name}`"


def derived_name(prefix: str, table: str, suffix: str = "") -> str:
    """Index/constraint name that can never exceed MySQL's 64 character limit.

    MySQL rejects identifiers longer than 64 characters (error 1059), and
    ``idx_<table>_uploaded_at`` on a 64 character table name would exceed it.  The
    tail is therefore truncated and disambiguated with a short hash, so two
    different long names cannot collide.  SQLite has no such limit, but it uses the
    same helper so both stores name the same objects identically.
    """
    name = f"{prefix}{table}{suffix}"
    if len(name) <= MAX_IDENTIFIER_CHARS:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    tail = f"_{digest}{suffix}"
    room = max(1, MAX_IDENTIFIER_CHARS - len(prefix) - len(tail))
    return f"{prefix}{table[:room]}{tail}"


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
def create_database_sql(database: str, charset: str = DEFAULT_CHARSET) -> str:
    """``CREATE DATABASE IF NOT EXISTS`` for the given (validated) names.

    The charset is validated rather than backtick-quoted: ``CHARACTER SET`` is one
    of the few places where MySQL is fussy about quoted identifiers.
    """
    schema = quote_identifier(database, label="Database name")
    if not _IDENTIFIER_PATTERN.match(charset or ""):
        raise InvalidDatabaseSettingsError(
            f"Charset {charset!r} is not a valid MySQL charset name."
        )
    return f"CREATE DATABASE IF NOT EXISTS {schema} CHARACTER SET {charset}"


def create_table_sql(table: str, charset: str = DEFAULT_CHARSET) -> str:
    """DDL for the parent table: one row per extraction, structured fields included."""
    name = quote_identifier(table, label="Table name")
    uploaded_index = derived_name("idx_", table, "_uploaded_at")
    filename_index = derived_name("idx_", table, "_filename")
    sha_index = derived_name("idx_", table, "_sha256")
    field_lines = "".join(
        f"\n  `{column}` {MYSQL_FIELD_COLUMN_TYPES[column]} NULL," for column in FIELD_ORDER
    )
    return f"""CREATE TABLE IF NOT EXISTS {name} (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `filename` VARCHAR({MAX_FILENAME_CHARS}) NOT NULL,
  `uploaded_at` DATETIME(6) NOT NULL,
  `content` LONGTEXT NOT NULL,
  `kind` VARCHAR(16) NOT NULL DEFAULT 'image',
  `page_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `char_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `word_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `confidence` DECIMAL(5,2) NULL,
  `duration_ms` INT UNSIGNED NOT NULL DEFAULT 0,
  `size_bytes` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `ocr_language` VARCHAR(64) NULL,
  `engine_version` VARCHAR(64) NULL,{field_lines}
  `content_sha256` CHAR(64) NULL,
  `stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `{uploaded_index}` (`uploaded_at`),
  KEY `{filename_index}` (`filename`),
  KEY `{sha_index}` (`content_sha256`)
) ENGINE=InnoDB DEFAULT CHARSET={charset}"""



def create_pages_table_sql(
    pages_table: str, extractions_table: str, charset: str = DEFAULT_CHARSET
) -> str:
    """DDL for the child table: one row per page, ``ON DELETE CASCADE``."""
    name = quote_identifier(pages_table, label="Pages table name")
    parent = quote_identifier(extractions_table, label="Table name")
    unique_key = derived_name("uq_", pages_table, "_page")
    constraint = derived_name("fk_", pages_table, "_extraction")
    return f"""CREATE TABLE IF NOT EXISTS {name} (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `extraction_id` BIGINT UNSIGNED NOT NULL,
  `page_number` INT UNSIGNED NOT NULL,
  `method` VARCHAR(16) NOT NULL DEFAULT 'ocr',
  `content` LONGTEXT NOT NULL,
  `char_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `word_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `confidence` DECIMAL(5,2) NULL,
  `duration_ms` INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `{unique_key}` (`extraction_id`, `page_number`),
  CONSTRAINT `{constraint}` FOREIGN KEY (`extraction_id`)
    REFERENCES {parent} (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET={charset}"""


def utc_now() -> datetime:
    """Current UTC time as a *naive* datetime (what ``DATETIME`` stores).

    Timestamps are always written in UTC, so a server in another time zone cannot
    shift the upload history; the UI and the API label the values as UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(uploaded_at: datetime | None) -> datetime:
    """*uploaded_at* as a naive UTC datetime (``now`` when it is ``None``)."""
    if uploaded_at is None:
        return utc_now()
    if uploaded_at.tzinfo is not None:
        return uploaded_at.astimezone(timezone.utc).replace(tzinfo=None)
    return uploaded_at


def content_sha256(text: str) -> str:
    """Fingerprint of the extracted text - handy for de-duplication queries."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def resolve_fields(
    result: ExtractionResult, fields: DocumentFields | None = None
) -> DocumentFields:
    """The structured fields to store: the reviewed ones, else the extracted ones."""
    return fields if fields is not None else result.structured_fields


def extraction_params(
    result: ExtractionResult,
    content: str,
    timestamp,
    fields: DocumentFields | None = None,
) -> tuple:
    """The bound values of one ``ocr_extractions`` row, in ``EXTRACTIONS_COLUMNS`` order.

    Both dialects build their ``INSERT`` from this one list, so a reviewed value can
    never be bound into the wrong column - and ``content_sha256`` stays last, where
    the tests (and the ``sql/`` scripts) expect the digest.

    *timestamp* is dialect specific (a ``datetime`` for MySQL, the UTC text for
    SQLite), which is why the caller passes it in.
    """
    reviewed = resolve_fields(result, fields).to_row()
    return (
        (result.filename or "upload")[:MAX_FILENAME_CHARS],
        timestamp,
        content,
        result.kind,
        result.page_count,
        result.char_count,
        result.word_count,
        result.confidence,
        result.duration_ms,
        result.size_bytes,
        result.languages,
        result.tesseract_version,
        *(reviewed[name] for name in FIELD_ORDER),
        content_sha256(content),
    )



def whole_number(
    value: object, default: int, *, label: str, minimum: int, maximum: int
) -> int:
    """Parse a bounded integer field, with a message a user can act on."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError as exc:
        raise InvalidDatabaseSettingsError(
            f"{label} must be a whole number between {minimum} and {maximum}, got {value!r}."
        ) from exc
    if not minimum <= number <= maximum:
        raise InvalidDatabaseSettingsError(
            f"{label} must be between {minimum} and {maximum}, got {number}."
        )
    return number


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MySqlSettings:
    """Validated connection details for one MySQL server.

    :meth:`to_public_dict` never exposes the password - that is the shape the UI
    and the JSON API receive back.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user: str = DEFAULT_USER
    password: str = ""
    database: str = DEFAULT_DATABASE
    table: str = DEFAULT_TABLE
    pages_table: str = f"{DEFAULT_TABLE}{PAGES_TABLE_SUFFIX}"
    charset: str = DEFAULT_CHARSET
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT

    def __post_init__(self) -> None:
        if self.table == self.pages_table:
            raise InvalidDatabaseSettingsError(
                "The per-page table must use a different name than the extractions table."
            )

    @classmethod
    def from_mapping(
        cls, data, *, defaults: "MySqlSettings | None" = None
    ) -> "MySqlSettings":
        """Build settings from a form (``MultiDict``) or a JSON object.

        Blank values fall back to *defaults*, with two deliberate exceptions: a
        missing host or user is an error (they are never guessed), and a blank
        password reuses the stored one so a secret never has to travel back into
        the page.
        """
        base = defaults or cls()

        def _text(key: str, fallback: str) -> str:
            raw = data.get(key)
            return fallback if raw is None else str(raw).strip()

        host = _text("host", base.host)
        if not host:
            raise InvalidDatabaseSettingsError(
                "Enter the host name of the MySQL server (for example 127.0.0.1)."
            )
        user = _text("user", base.user)
        if not user:
            raise InvalidDatabaseSettingsError(
                "Enter the MySQL user name (for example root)."
            )

        database = sanitize_identifier(
            _text("database", base.database), DEFAULT_DATABASE, label="Database name"
        )
        table = sanitize_identifier(
            _text("table", base.table), DEFAULT_TABLE, label="Table name"
        )
        pages_table = sanitize_identifier(
            _text("pages_table", "") or f"{table}{PAGES_TABLE_SUFFIX}",
            f"{DEFAULT_TABLE}{PAGES_TABLE_SUFFIX}",
            label="Pages table name",
        )
        charset = sanitize_identifier(
            _text("charset", base.charset), DEFAULT_CHARSET, label="Charset"
        )

        raw_password = data.get("password")
        password = base.password if raw_password is None or str(raw_password) == "" else str(raw_password)

        return cls(
            host=host,
            port=whole_number(
                data.get("port"), base.port, label="Port", minimum=1, maximum=65535
            ),
            user=user,
            password=password,
            database=database,
            table=table,
            pages_table=pages_table,
            charset=charset,
            connect_timeout=whole_number(
                data.get("connect_timeout"),
                base.connect_timeout,
                label="Connect timeout",
                minimum=1,
                maximum=120,
            ),
        )

    @property
    def connection_label(self) -> str:
        """``user@host:port/database`` - safe to show, never contains a secret."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"

    def to_public_dict(self) -> dict:
        """Everything the UI/API may see (password replaced by a flag)."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "table": self.table,
            "pages_table": self.pages_table,
            "charset": self.charset,
            "connect_timeout": self.connect_timeout,
            "has_password": bool(self.password),
            "label": self.connection_label,
        }

    def to_storage_dict(self) -> dict:
        """Full dictionary written to the optional "remember me" file."""
        data = self.to_public_dict()
        data.pop("has_password", None)
        data.pop("label", None)
        data["password"] = self.password
        return data


# ---------------------------------------------------------------------------
# row helpers
# ---------------------------------------------------------------------------
def serialise_timestamp(value: object) -> str | None:
    """Render a stored timestamp; every value we write is naive UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M:%S} UTC"
    return str(value)


def serialise_row(row) -> dict:
    """Make a cursor row safe for Jinja and ``jsonify``.

    ``DictCursor`` hands back ``datetime``, ``date``, ``Decimal`` and ``bytes``
    objects; templates and JSON both prefer strings/floats.  ``app.sqlite`` reuses
    this on ``sqlite3.Row`` objects, which is why it is public.  ``datetime`` is
    checked **before** ``date`` because it is a subclass of it.
    """
    if row is None:
        return {}
    serialised: dict = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            value = serialise_timestamp(value)
        elif isinstance(value, date):
            # The structured fields: a MySQL DATE column comes back as a date object,
            # which neither Jinja nor jsonify accepts.
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        serialised[str(key)] = value
    return serialised



def clamp_record_limit(limit: object, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Turn user supplied limits into something MySQL can safely bind.

    Anything unusable (blank, ``0``, ``-3``, ``nonsense``) falls back to *default* -
    the records view passes the configured page size, so a hand written ``?limit=``
    can neither widen a page beyond :data:`MAX_LIST_LIMIT` nor empty it.
    """
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, MAX_LIST_LIMIT)


def clamp_record_page(page: object) -> int:
    """Turn a user supplied ``?page=`` into a 1-based page number.

    Anything unusable (blank, ``0``, ``-3``, ``nonsense``) means page 1: a stale
    bookmark must still show records instead of an error page.
    """
    raw = "" if page is None else str(page).strip()
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, min(value, MAX_RECORD_PAGE))


def clamp_record_offset(offset: object) -> int:
    """Turn a user supplied offset into rows a database can safely skip."""
    raw = "" if offset is None else str(offset).strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, min(value, (MAX_RECORD_PAGE - 1) * MAX_LIST_LIMIT))


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------
def clean_search_term(query: object) -> str:
    """Normalise a search term; ``""`` means "no search, list everything".

    Whitespace is collapsed (a search box cannot contain a newline, but stored
    content can) and the term is truncated so a pathological pattern never reaches
    MySQL.
    """
    term = "" if query is None else " ".join(str(query).split())
    return term[:MAX_SEARCH_CHARS]


def normalize_search_scope(scope: object) -> str:
    """A known scope, or :data:`DEFAULT_SEARCH_SCOPE` for anything else.

    Unknown values fall back instead of raising: a stale bookmark must still show
    the records, and the UI only ever sends the three known values.
    """
    candidate = "" if scope is None else str(scope).strip().lower()
    return candidate if candidate in _SEARCH_COLUMNS else DEFAULT_SEARCH_SCOPE


def like_pattern(term: str) -> str:
    """``%term%`` with ``!``, ``%`` and ``_`` escaped so they match literally.

    The escape character itself is doubled first, so ``!`` in a search term stays a
    ``!`` and cannot neutralise the escaping of the next character.
    """
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def record_id_from_term(term: str) -> int | None:
    """The record id a purely numeric term refers to (``None`` when it is not one).

    Searching for ``42`` finds record #42 as well as any text containing "42"; the
    value is bounded because a longer number cannot be a ``BIGINT UNSIGNED`` id.
    """
    if not term.isdigit():
        return None
    value = int(term)
    return value if value <= MAX_RECORD_ID else None


def search_clause(
    query: object, scope: object = DEFAULT_SEARCH_SCOPE, *, placeholder: str = "%s"
) -> tuple[str, tuple]:
    """``(WHERE ..., params)`` for a search term - ``("", ())`` lists everything.

    One clause drives every listing: the records view, its JSON API twin and the
    Excel export, so they can never disagree about what a term matches.  The clause
    is identical in both dialects (backticks and ``ESCAPE`` included), so
    *placeholder* only picks the driver's parameter style - ``%s`` for PyMySQL,
    ``?`` for SQLite.
    """
    term = clean_search_term(query)
    if not term:
        return "", ()

    columns = _SEARCH_COLUMNS[normalize_search_scope(scope)]
    conditions = " OR ".join(
        f"`{column}` LIKE {placeholder} ESCAPE '{LIKE_ESCAPE}'" for column in columns
    )
    where = f" WHERE ({conditions} OR `id` = {placeholder})"
    params = (*(like_pattern(term) for _ in columns), record_id_from_term(term))
    return where, params


# ---------------------------------------------------------------------------
# connection + schema
# ---------------------------------------------------------------------------
class MySqlDatabase:
    """One live PyMySQL connection plus the schema it created.

    PyMySQL connections are not thread safe, so every statement is serialised
    behind an ``RLock``.  Cursors use ``DictCursor``, which keeps the mapping to
    the templates and to ``jsonify`` trivial.

    :class:`app.sqlite.SqliteDatabase` implements the very same public surface, so
    :class:`DatabaseManager` and the routes treat both alike.
    """

    #: Read by the UI/API instead of asking the class - the SQLite store sets it too.
    provider = PROVIDER

    def __init__(self, settings: MySqlSettings) -> None:
        self.settings = settings
        self.server_version: str | None = None
        self.connected_at: datetime | None = None
        self.last_error: str | None = None
        #: True when *this* connect created the schema (shown in the UI).
        self.database_created = False
        #: True when at least one of the two tables did not exist yet.
        self.tables_created = False
        self._connection = None
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> "MySqlDatabase":
        """Open the server, create the schema when needed and select it.

        The database comes from ``CREATE DATABASE IF NOT EXISTS`` and the tables
        from ``CREATE TABLE IF NOT EXISTS``, so connecting is always safe to
        repeat - and it is everything an empty MySQL instance needs.
        """
        _require_driver()
        with self._lock:
            self.close()
            try:
                self._connection = self._open()
                self.database_created = self._create_database()
                self._select_database()
                self.tables_created = self._create_tables()
                self.server_version = self._server_version()
            except DatabaseError:
                raise
            except _mysql_error_types() as exc:
                self.last_error = str(exc)
                self.close()
                raise DatabaseUnavailableError(
                    f"Could not connect to MySQL at {self.settings.connection_label}: {exc}"
                ) from exc
            self.last_error = None
            self.connected_at = utc_now()

        logger.info(
            "MySQL ready: %s (server %s, schema %s, tables %s)",
            self.settings.connection_label,
            self.server_version,
            "created" if self.database_created else "already present",
            "created" if self.tables_created else "already present",
        )
        return self

    def ensure_schema(self) -> bool:
        """Create whatever is missing on the *current* connection."""
        with self._lock:
            self._ensure_healthy()
            try:
                self._create_database()
                self._select_database()
                created = self._create_tables()
            except _mysql_error_types() as exc:
                raise self._failure("Creating the MySQL schema", exc) from exc
        self.tables_created = created or self.tables_created
        return created

    def close(self) -> None:
        """Drop the connection quietly (never raises)."""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception as exc:  # noqa: BLE001 - closing must never propagate
            logger.debug("Ignoring error while closing the MySQL connection: %s", exc)

    # -- low level -------------------------------------------------------
    def _open(self):
        assert pymysql is not None  # guarded by _require_driver()
        return pymysql.connect(
            host=self.settings.host,
            port=self.settings.port,
            user=self.settings.user,
            password=self.settings.password,
            charset=self.settings.charset,
            connect_timeout=self.settings.connect_timeout,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _require_connection(self):
        if self._connection is None:
            raise DatabaseNotConfiguredError(
                "No MySQL server is connected. Open the Database page and connect first."
            )
        return self._connection

    def _ensure_healthy(self):
        """Return a usable connection, reconnecting once when the server dropped us."""
        connection = self._require_connection()
        try:
            connection.ping(reconnect=True)
        except Exception as exc:  # noqa: BLE001 - any driver error means "unusable"
            self.last_error = str(exc)
            raise DatabaseUnavailableError(
                f"The MySQL connection to {self.settings.connection_label} was lost: {exc}"
            ) from exc
        return connection

    def _failure(self, action: str, exc: Exception) -> DatabaseError:
        self.last_error = str(exc)
        return DatabaseWriteError(
            f"{action} failed on {self.settings.connection_label}: {exc}"
        )

    def _execute(self, sql: str, params=None) -> int:
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params)
                return cursor.rowcount
            finally:
                cursor.close()

    def _fetchone(self, sql: str, params=None):
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params)
                return cursor.fetchone()
            finally:
                cursor.close()

    def _fetchall(self, sql: str, params=None) -> list:
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
            finally:
                cursor.close()

    # -- schema ----------------------------------------------------------
    def _create_database(self) -> bool:
        """Create the schema when missing; True when *this* call created it."""
        existed = self._schema_exists()
        self._execute(create_database_sql(self.settings.database, self.settings.charset))
        return existed is False

    def _schema_exists(self) -> bool | None:
        """``True``/``False``, or ``None`` when the server would not tell us."""
        try:
            row = self._fetchone(
                "SELECT `SCHEMA_NAME` FROM `information_schema`.`SCHEMATA` "
                "WHERE `SCHEMA_NAME` = %s",
                (self.settings.database,),
            )
        except _mysql_error_types() as exc:
            logger.debug("Schema probe failed (%s); assuming it does not exist yet", exc)
            return None
        return row is not None

    def _create_tables(self) -> bool:
        """Run both ``CREATE TABLE IF NOT EXISTS`` statements."""
        created = False
        statements = (
            (
                self.settings.table,
                create_table_sql(self.settings.table, self.settings.charset),
            ),
            (
                self.settings.pages_table,
                create_pages_table_sql(
                    self.settings.pages_table, self.settings.table, self.settings.charset
                ),
            ),
        )
        for name, statement in statements:
            existed = self._table_exists(name)
            self._execute(statement)
            if existed is False:
                created = True
        self._add_field_columns()
        return created

    def _table_columns(self, table: str) -> set[str]:
        """The column names of *table* (empty when it cannot be read)."""
        try:
            rows = self._fetchall(
                "SELECT `COLUMN_NAME` FROM `information_schema`.`COLUMNS` "
                "WHERE `TABLE_SCHEMA` = %s AND `TABLE_NAME` = %s",
                (self.settings.database, table),
            )
        except _mysql_error_types() as exc:
            logger.debug("Column probe for %s failed (%s); assuming none", table, exc)
            return set()
        return {str((row or {}).get("COLUMN_NAME") or "") for row in rows}

    def _add_field_columns(self, table: str | None = None) -> tuple[str, ...]:
        """Add the structured field columns to a table that predates them.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists,
        so a store created before the structured fields were introduced would keep
        rejecting every save.  MySQL (unlike MariaDB) has no ``ADD COLUMN IF NOT
        EXISTS``, hence the ``information_schema`` probe; it runs on every connect
        and on **Create schema**, so an older table is upgraded in place and no data
        is lost.
        """
        target = table or self.settings.table
        existing = self._table_columns(target)
        if not existing:
            return ()
        missing = tuple(name for name in FIELD_ORDER if name not in existing)
        if not missing:
            return ()
        name = quote_identifier(target, label="Table name")
        for column in missing:
            self._execute(
                f"ALTER TABLE {name} ADD COLUMN `{column}` "
                f"{MYSQL_FIELD_COLUMN_TYPES[column]} NULL"
            )
        logger.info("Added %s structured field column(s) to %s", len(missing), target)
        return missing


    def _table_exists(self, table: str) -> bool | None:
        try:
            row = self._fetchone(
                "SELECT `TABLE_NAME` FROM `information_schema`.`TABLES` "
                "WHERE `TABLE_SCHEMA` = %s AND `TABLE_NAME` = %s",
                (self.settings.database, table),
            )
        except _mysql_error_types() as exc:
            logger.debug("Table probe for %s failed (%s); assuming it is new", table, exc)
            return None
        return row is not None

    def _select_database(self) -> None:
        """``USE`` the schema and prove the server really switched."""
        connection = self._require_connection()
        connection.select_db(self.settings.database)
        row = self._fetchone("SELECT DATABASE() AS `current_database`")
        current = (row or {}).get("current_database")
        if current and current != self.settings.database:
            raise DatabaseUnavailableError(
                f"MySQL selected {current!r} instead of {self.settings.database!r}."
            )

    def _server_version(self) -> str | None:
        row = self._fetchone("SELECT VERSION() AS `server_version`")
        return str((row or {}).get("server_version")) if row else None

    # -- writes ----------------------------------------------------------
    def save_extraction(
        self,
        result: ExtractionResult,
        *,
        uploaded_at: datetime | None = None,
        fields: DocumentFields | None = None,
    ) -> int:
        """Insert one extraction (plus its pages) and return the new record id.

        Both inserts share a transaction, so a failure leaves no half-written
        record behind.  ``uploaded_at`` defaults to "now" in UTC.  *fields* are the
        **reviewed** structured values; without it the ones the parser proposed are
        stored (see :func:`resolve_fields`).
        """
        content = result.full_text()
        timestamp = as_utc(uploaded_at)
        columns = ", ".join(f"`{name}`" for name in EXTRACTIONS_COLUMNS)
        placeholders = ", ".join(["%s"] * len(EXTRACTIONS_COLUMNS))
        sql = (
            f"INSERT INTO {quote_identifier(self.settings.table)} "
            f"({columns}) VALUES ({placeholders})"
        )
        params = extraction_params(result, content, timestamp, fields)


        with self._lock:
            connection = self._ensure_healthy()
            cursor = connection.cursor()
            try:
                connection.begin()
                cursor.execute(sql, params)
                record_id = int(cursor.lastrowid)
                page_count = self._insert_pages(cursor, record_id, result)
                connection.commit()
            except Exception as exc:
                self._rollback(connection)
                raise self._failure("Saving the extraction", exc) from exc
            finally:
                cursor.close()

        logger.debug(
            "Stored %r as %s record %s (%s page row(s))",
            result.filename,
            self.settings.table,
            record_id,
            page_count,
        )
        return record_id

    def _insert_pages(self, cursor, record_id: int, result: ExtractionResult) -> int:
        """``INSERT`` one row per page - the numbers come straight from the pipeline."""
        if not result.pages:
            return 0
        columns = ", ".join(["`extraction_id`"] + [f"`{name}`" for name in PAGES_COLUMNS])
        placeholders = ", ".join(["%s"] * (len(PAGES_COLUMNS) + 1))
        sql = (
            f"INSERT INTO {quote_identifier(self.settings.pages_table)} "
            f"({columns}) VALUES ({placeholders})"
        )
        rows = [
            (
                record_id,
                page.page_number,
                page.method,
                page.text,
                page.char_count,
                page.word_count,
                page.confidence,
                page.duration_ms,
            )
            for page in result.pages
        ]
        cursor.executemany(sql, rows)
        return len(rows)

    def delete_extraction(self, record_id: int) -> bool:
        """Delete one record; its page rows cascade.  ``False`` when unknown."""
        table = quote_identifier(self.settings.table)
        with self._lock:
            self._ensure_healthy()
            try:
                removed = self._execute(f"DELETE FROM {table} WHERE `id` = %s", (int(record_id),))
            except _mysql_error_types() as exc:
                raise self._failure("Deleting the stored extraction", exc) from exc
        return bool(removed)

    @staticmethod
    def _rollback(connection) -> None:
        try:
            connection.rollback()
        except Exception as exc:  # noqa: BLE001 - the original error matters more
            logger.debug("Rollback failed: %s", exc)

    # -- reads -----------------------------------------------------------
    def record_count(self) -> int:
        """How many extractions are stored (used by the status panel)."""
        table = quote_identifier(self.settings.table)
        with self._lock:
            self._ensure_healthy()
            try:
                row = self._fetchone(f"SELECT COUNT(*) AS `total` FROM {table}")
            except _mysql_error_types() as exc:
                raise self._failure("Counting the stored extractions", exc) from exc
        return int((row or {}).get("total") or 0)

    def recent_extractions(
        self, limit: object = DEFAULT_LIST_LIMIT, *, offset: object = 0
    ) -> list[dict]:
        """Newest first, without dragging whole documents into the page.

        *offset* skips that many rows first, so a caller can walk the list page by
        page (``LIMIT``/``OFFSET``) instead of reading everything.
        """
        return self._list_extractions("", (), limit, offset=offset)

    def search_extractions(
        self,
        query: object = "",
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        scope: object = DEFAULT_SEARCH_SCOPE,
        offset: object = 0,
    ) -> list[dict]:
        """Stored records whose file name, text or id matches *query*.

        The term is matched case-insensitively (``utf8mb4`` collations are), may
        contain ``%``/``_``/``!`` without turning into wildcards, and a purely numeric
        term also finds that record number.  An empty term is not an error: it falls
        back to :meth:`recent_extractions`, so a caller can drive both the plain list
        and a search through this one method.

        *offset* is the ``OFFSET`` of that one query - the records view passes
        ``(page - 1) * limit`` so every page is a single query that reads no more
        than ``limit`` rows.
        """
        where, params = search_clause(query, scope)
        return self._list_extractions(where, params, limit, offset=offset)

    def count_extractions(
        self, query: object = "", *, scope: object = DEFAULT_SEARCH_SCOPE
    ) -> int:
        """How many records match *query* - the row count behind the page buttons.

        It runs the very same :func:`search_clause` as :meth:`search_extractions`,
        so a page count can never disagree with the rows it is counting.
        """
        where, params = search_clause(query, scope)
        table = quote_identifier(self.settings.table)
        with self._lock:
            self._ensure_healthy()
            try:
                row = self._fetchone(f"SELECT COUNT(*) AS `total` FROM {table}{where}", params)
            except _mysql_error_types() as exc:
                raise self._failure("Counting the matching extractions", exc) from exc
        return int((row or {}).get("total") or 0)

    def export_extractions(
        self,
        query: object = "",
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        scope: object = DEFAULT_SEARCH_SCOPE,
        offset: object = 0,
    ) -> list[dict]:
        """The rows the Excel export writes: the same search, with the whole text.

        Identical filtering and ordering to :meth:`search_extractions` (both go
        through :func:`search_clause`), but ``content`` is selected instead of a
        ``LEFT()`` preview - an export is there to be read, not to be listed.
        """
        where, params = search_clause(query, scope)
        return self._list_extractions(
            where, params, limit, offset=offset, columns=EXPORT_COLUMNS, preview_chars=None
        )

    def pages_for_extractions(self, record_ids: Sequence[int]) -> list[dict]:
        """Every stored page of the given records, one query, ordered by record/page.

        The ids come from our own listing (at most :data:`MAX_LIST_LIMIT`), and they
        are bound as parameters exactly like the search patterns - the ``IN`` list is
        never interpolated into the SQL.
        """
        identifiers = [int(value) for value in record_ids]
        if not identifiers:
            return []
        placeholders = ", ".join(["%s"] * len(identifiers))
        pages_table = quote_identifier(self.settings.pages_table)
        sql = (
            f"SELECT {EXPORT_PAGE_COLUMNS} FROM {pages_table} "
            f"WHERE `extraction_id` IN ({placeholders}) "
            "ORDER BY `extraction_id` ASC, `page_number` ASC"
        )
        with self._lock:
            self._ensure_healthy()
            try:
                rows = self._fetchall(sql, tuple(identifiers))
            except _mysql_error_types() as exc:
                raise self._failure("Loading the stored pages", exc) from exc
        return [serialise_row(row) for row in rows]

    def _list_extractions(
        self,
        where: str,
        params: tuple,
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        offset: object = 0,
        columns: str = LIST_COLUMNS,
        preview_chars: int | None = RECORD_PREVIEW_CHARS,
    ) -> list[dict]:
        """One listing query behind the list, the search and the Excel export.

        ``columns`` says what to select and ``preview_chars`` whether a preview length
        is bound in front of the search parameters (``None`` selects the full text).
        *offset* adds an ``OFFSET`` for paging - it is only appended from page 2 on,
        so the first page runs exactly the query it always did.
        """
        safe_limit = clamp_record_limit(limit)
        safe_offset = clamp_record_offset(offset)
        table = quote_identifier(self.settings.table)
        sql = (
            f"SELECT {columns} FROM {table}{where} "
            "ORDER BY `uploaded_at` DESC, `id` DESC LIMIT %s"
        )
        bound = (*((preview_chars,) if preview_chars is not None else ()), *params, safe_limit)
        if safe_offset:
            sql += " OFFSET %s"
            bound = (*bound, safe_offset)
        with self._lock:
            self._ensure_healthy()
            try:
                rows = self._fetchall(sql, bound)
            except _mysql_error_types() as exc:
                raise self._failure("Listing the stored extractions", exc) from exc
        return [serialise_row(row) for row in rows]

    def get_extraction(self, record_id: int) -> dict | None:
        """One record including the full text and its per-page rows."""
        identifier = int(record_id)
        table = quote_identifier(self.settings.table)
        pages_table = quote_identifier(self.settings.pages_table)
        with self._lock:
            self._ensure_healthy()
            try:
                parent = self._fetchone(f"SELECT * FROM {table} WHERE `id` = %s", (identifier,))
                if parent is None:
                    return None
                pages = self._fetchall(
                    "SELECT `page_number`, `method`, `content`, `char_count`, `word_count`, "
                    f"`confidence`, `duration_ms` FROM {pages_table} "
                    "WHERE `extraction_id` = %s ORDER BY `page_number` ASC",
                    (identifier,),
                )
            except _mysql_error_types() as exc:
                raise self._failure("Loading the stored extraction", exc) from exc
        record = serialise_row(parent)
        record["pages"] = [serialise_row(page) for page in pages]
        return record

    def require_extraction(self, record_id: int) -> dict:
        """Like :meth:`get_extraction`, but raises the 404 the UI wants."""
        record = self.get_extraction(record_id)
        if record is None:
            raise DatabaseRecordNotFoundError(
                f"Record #{int(record_id)} is not stored in "
                f"{self.settings.database}.{self.settings.table}."
            )
        return record

    # -- reporting -------------------------------------------------------
    def status(self) -> dict:
        """A JSON friendly summary - never runs a query, so it is always safe."""
        return {
            "provider": PROVIDER,
            "connected": self._connection is not None,
            "connection_label": self.settings.connection_label,
            "settings": self.settings.to_public_dict(),
            "server_version": self.server_version,
            "database_created": self.database_created,
            "tables_created": self.tables_created,
            "connected_at": serialise_timestamp(self.connected_at),
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------
class DatabaseManager:
    """Owns the *currently connected* store plus the remembered credentials.

    A Flask app has exactly one of these (``app.extensions["ocr_database"]``).
    The OCR pipeline is stateless, so a single shared connection is enough and it
    keeps "which store are we writing to?" in one obvious place.  Connecting is
    explicit (the ``/database`` page, its JSON API, ``DATABASE_AUTO_CONNECT``) -
    importing the app never touches a server or a file.

    Two stores can be connected, one at a time:

    ``mysql``
        A server described by :class:`MySqlSettings`; the schema and the tables are
        created on connect.
    ``sqlite``
        One local file (:class:`app.sqlite.SqliteSettings`), created on connect -
        no server, no credentials.  This is what makes testing (and a machine
        without MySQL) work: ``DATABASE_BACKEND=sqlite``, or the *Connect SQLite*
        button on the Database page.
    """

    def __init__(
        self,
        *,
        defaults: MySqlSettings | None = None,
        settings_file: str | Path | None = None,
        sqlite_defaults=None,
        backend: object = None,
    ) -> None:
        self._lock = threading.RLock()
        self._defaults = defaults or MySqlSettings()
        self.settings_file = Path(settings_file) if settings_file else None
        #: ``SqliteSettings``, built by ``create_app`` (app.sqlite uses this module).
        self._sqlite_defaults = sqlite_defaults
        self._backend = normalize_backend(backend, DEFAULT_BACKEND)
        self._database = None

    # -- accessors -------------------------------------------------------
    @property
    def defaults(self) -> MySqlSettings:
        """Values from the environment, used to pre-fill the connection form."""
        return self._defaults

    @property
    def configured_backend(self) -> str:
        """What ``DATABASE_BACKEND`` asked for (``auto``/``mysql``/``sqlite``)."""
        return self._backend

    @property
    def backend(self) -> str:
        """The store a connect would use *now*: the live one, else the configured."""
        database = self._database
        if database is not None:
            return database.provider
        return self._backend

    @property
    def labels(self) -> Backend:
        """How the UI names the store (``MySQL`` while ``auto`` has nothing live)."""
        return backend_labels(self.backend)

    @property
    def database(self):
        """The live store (``MySqlDatabase`` or ``SqliteDatabase``), else ``None``."""
        return self._database

    @property
    def is_connected(self) -> bool:
        return self._database is not None

    def require_database(self):
        """The live connection, or a clear error the UI can show."""
        if self._database is None:
            if self.backend == BACKEND_SQLITE:
                raise DatabaseNotConfiguredError(
                    "No SQLite database is connected yet. Open the Database page and "
                    "connect one first - the file and its tables are created for you "
                    "when you connect."
                )
            raise DatabaseNotConfiguredError(
                "No MySQL server is connected yet. Open the Database page and connect "
                "one first - the schema is created for you when you connect."
            )
        return self._database

    # -- connect / disconnect --------------------------------------------
    def suggest_settings(self) -> MySqlSettings:
        """What the connection form starts with: saved values, else environment."""
        return self.saved_settings() or self._defaults

    def suggest_sqlite_settings(self, data=None):
        """SQLite settings for the form: the submitted values over the defaults.

        *data* is a form (``MultiDict``) or a JSON object; ``None`` simply returns
        what the configuration asked for.
        """
        module = _sqlite_module()
        base = self._sqlite_defaults or module.SqliteSettings()
        if data is None:
            return base
        return module.SqliteSettings.from_mapping(data, defaults=base)

    def form_defaults(self) -> dict:
        """Pre-fill data for the form (never the password itself)."""
        data = self.suggest_settings().to_public_dict()
        data["saved"] = self.saved_settings() is not None
        return data

    def connect(self, data, *, remember: bool | None = None, backend: object = None):
        """Connect the requested store, creating the schema when it is missing.

        *data* is a form (``MultiDict``) or a JSON object; it is merged over the
        suggested settings, so the UI only sends what changed.  ``backend`` overrides
        the ``backend`` field of *data*, which in turn falls back to the configured
        ``DATABASE_BACKEND``: ``sqlite`` goes to :meth:`connect_sqlite`, everything
        else (including ``auto``, which asks MySQL first) to :meth:`connect_mysql`.

        The previous connection is dropped only once the new one is up, so a typo
        cannot take a working setup offline.
        """
        if self._requested_backend(data, backend) == BACKEND_SQLITE:
            return self.connect_sqlite(data)
        return self.connect_mysql(data, remember=remember)

    def connect_mysql(self, data, *, remember: bool | None = None) -> MySqlDatabase:
        """Connect the MySQL server described by *data* (or by *data* itself)."""
        settings = (
            data
            if isinstance(data, MySqlSettings)
            else MySqlSettings.from_mapping(data, defaults=self.suggest_settings())
        )
        database = MySqlDatabase(settings).connect()
        self._replace(database)
        if remember is None or remember:
            self.save_settings(settings)
        return database

    def connect_sqlite(self, data=None):
        """Open (and create) the local SQLite file - no server, no credentials.

        *data* may carry a ``path`` (and table names) for the file; without it the
        configured :class:`app.sqlite.SqliteSettings` are used.
        """
        settings = self.suggest_sqlite_settings(data)
        database = _sqlite_module().SqliteDatabase(settings).connect()
        self._replace(database)
        # Keep what just worked as the suggestion for the next connect: a path is not
        # a secret, and a form that forgets it would be annoying (the MySQL form has
        # the same memory, on disk).
        self._sqlite_defaults = settings
        logger.info("Connected the SQLite store at %s.", settings.label)
        return database

    def auto_connect(self, *, remember: bool = False):
        """Connect what the configuration asks for - used at start-up.

        With ``auto`` the MySQL server is tried first; when it is not there the
        configured :meth:`connect_sqlite` fallback is used, so a development machine
        (or a test run) without MySQL still starts with a working store.  The
        attempt is logged either way - start-up must never fail because of it.
        """
        if self._backend == BACKEND_SQLITE:
            return self.connect_sqlite()
        try:
            return self.connect_mysql(self.suggest_settings(), remember=remember)
        except DatabaseUnavailableError as exc:
            if self._backend != BACKEND_AUTO or not _sqlite_available():
                raise
            logger.warning("MySQL is not available (%s); using SQLite instead.", exc)
            return self.connect_sqlite()

    def _replace(self, database) -> None:
        """Make *database* the live store and close whatever it replaces."""
        with self._lock:
            previous, self._database = self._database, database
        if previous is not None and previous is not database:
            previous.close()

    def _requested_backend(self, data, backend: object = None) -> str:
        """Which backend the caller asked for: the argument, the field, else config."""
        if backend is not None:
            return normalize_backend(backend, self._backend)
        if isinstance(data, MySqlSettings):
            return BACKEND_MYSQL  # a settings object *is* a MySQL request
        value = data.get("backend") if hasattr(data, "get") else None
        if value is not None and str(value).strip():
            return normalize_backend(value, self._backend)
        return self._backend

    def disconnect(self) -> None:
        """Close the connection; remembered credentials stay on disk."""
        with self._lock:
            database, self._database = self._database, None
        if database is not None:
            database.close()
            logger.info(
                "%s connection to %s closed.",
                "MySQL" if database.provider == BACKEND_MYSQL else "SQLite",
                database.settings.connection_label,
            )

    def ensure_schema(self) -> bool:
        """Create a missing database/table on the live connection."""
        return self.require_database().ensure_schema()

    # -- delegating queries ----------------------------------------------
    def save_extraction(self, result: ExtractionResult, **kwargs) -> int:
        return self.require_database().save_extraction(result, **kwargs)

    def record_count(self) -> int:
        return self.require_database().record_count()

    def recent_extractions(
        self, limit: object = DEFAULT_LIST_LIMIT, *, offset: object = 0
    ) -> list[dict]:
        return self.require_database().recent_extractions(limit, offset=offset)

    def search_extractions(
        self,
        query: object = "",
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        scope: object = DEFAULT_SEARCH_SCOPE,
        offset: object = 0,
    ) -> list[dict]:
        return self.require_database().search_extractions(
            query, limit, scope=scope, offset=offset
        )

    def count_extractions(
        self, query: object = "", *, scope: object = DEFAULT_SEARCH_SCOPE
    ) -> int:
        """How many records match the same search (see ``MySqlDatabase``)."""
        return self.require_database().count_extractions(query, scope=scope)

    def export_extractions(
        self,
        query: object = "",
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        scope: object = DEFAULT_SEARCH_SCOPE,
        offset: object = 0,
    ) -> list[dict]:
        """The rows of the Excel export: same filters, full text (see ``MySqlDatabase``)."""
        return self.require_database().export_extractions(
            query, limit, scope=scope, offset=offset
        )

    def pages_for_extractions(self, record_ids: Sequence[int]) -> list[dict]:
        return self.require_database().pages_for_extractions(record_ids)

    def get_extraction(self, record_id: int) -> dict | None:
        return self.require_database().get_extraction(record_id)

    def require_extraction(self, record_id: int) -> dict:
        return self.require_database().require_extraction(record_id)

    def delete_extraction(self, record_id: int) -> bool:
        return self.require_database().delete_extraction(record_id)

    # -- remembered credentials ------------------------------------------
    def saved_settings(self) -> MySqlSettings | None:
        """Read the optional "remember me" file (``None`` when absent/unusable)."""
        path = self.settings_file
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("the file must contain a JSON object")
            return MySqlSettings.from_mapping(payload)
        except (OSError, ValueError, InvalidDatabaseSettingsError) as exc:
            logger.warning("Ignoring unusable saved MySQL settings in %s: %s", path, exc)
            return None

    def save_settings(self, settings: MySqlSettings) -> bool:
        """Persist the connection details (including the password).

        The file lives in the Flask instance folder, is never served, and gets
        mode 600 on POSIX.  It is a convenience, not a secret store - "forget"
        on the ``/database`` page deletes it.
        """
        path = self.settings_file
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(settings.to_storage_dict(), indent=2) + "\n", encoding="utf-8"
            )
            if os.name == "posix":  # pragma: no cover - Windows keeps its ACLs
                os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("Could not remember the MySQL connection in %s: %s", path, exc)
            return False
        logger.debug("Remembered the MySQL connection in %s", path)
        return True

    def forget_settings(self) -> bool:
        """Delete the remembered credentials (a live connection is untouched)."""
        path = self.settings_file
        if path is None or not path.is_file():
            return False
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            return False
        return True

    # -- reporting -------------------------------------------------------
    def status(self) -> dict:
        """Status for the UI/API: the live store, both backends and the saved file.

        ``provider`` is the connected store (``mysql``/``sqlite``), or - while
        nothing is connected - what the configured backend would use, so the pages
        can name the store before it exists.  ``driver`` keeps describing PyMySQL
        (that is what the MySQL form needs); ``backends`` carries what the UI offers.
        """
        database = self._database
        if database is not None:
            payload = database.status()
        else:
            payload = {
                "connected": False,
                "connection_label": None,
                "settings": self.suggest_store_settings(),
                "server_version": None,
                "database_created": False,
                "tables_created": False,
                "connected_at": None,
                "last_error": None,
            }
        payload.update(
            {
                "provider": self.labels.id,
                "backend": self._backend,
                "backends": self._backend_status(),
                "driver": {
                    "name": DRIVER_NAME,
                    "available": driver_available(),
                    "version": driver_version(),
                },
                "install_hint": None if driver_available() else _INSTALL_HINT,
                "saved_settings": self.saved_settings() is not None,
                "settings_file": str(self.settings_file) if self.settings_file else None,
                "defaults": self._defaults.to_public_dict(),
            }
        )
        return payload

    def suggest_store_settings(self) -> dict:
        """The settings of the store the configured backend would use."""
        if self._backend == BACKEND_SQLITE and _sqlite_available():
            return self.suggest_sqlite_settings().to_public_dict()
        return self.suggest_settings().to_public_dict()

    def _backend_status(self) -> dict:
        """What the Database page may offer, per backend."""
        driver = driver_available()
        offer = {
            BACKEND_MYSQL: {
                "label": STORES[BACKEND_MYSQL].label,
                "available": driver,
                "settings": self.suggest_settings().to_public_dict(),
                "install_hint": None if driver else _INSTALL_HINT,
            }
        }
        if _sqlite_available():
            module = _sqlite_module()
            settings = self.suggest_sqlite_settings()
            offer[BACKEND_SQLITE] = {
                "label": STORES[BACKEND_SQLITE].label,
                "available": True,
                "settings": settings.to_public_dict(),
                "path": str(settings.path),
                "driver": {
                    "name": module.DRIVER_NAME,
                    "available": True,
                    "version": module.driver_version(),
                },
            }
        else:  # pragma: no cover - a Python built without sqlite3
            offer[BACKEND_SQLITE] = {
                "label": STORES[BACKEND_SQLITE].label,
                "available": False,
                "settings": None,
                "path": None,
            }
        return offer



