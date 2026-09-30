"""Tests for the SQLite store - the backend that needs no MySQL server.

Unlike ``tests/test_database.py`` (which drives a fake PyMySQL connection), these
tests talk to a **real** database: ``sqlite3`` ships with Python, so the whole
storage path - schema creation, saving, searching, the records view, the Excel
export - can be exercised end to end, on any machine, with nothing installed.

The fixtures point the application at a fresh file in ``tmp_path`` (or at
``:memory:``), so no test can see another test's rows.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone

import pytest

from app import create_app
from app.database import (
    BACKEND_MYSQL,
    BACKEND_SQLITE,
    DatabaseManager,
    backend_labels,
    content_sha256,
    normalize_backend,
)
from app.exceptions import (
    DatabaseNotConfiguredError,
    DatabaseRecordNotFoundError,
    DatabaseUnavailableError,
    DatabaseWriteError,
    InvalidDatabaseSettingsError,
)
from app.fields import DocumentFields
from app.ocr import ExtractionResult, PageResult
from app.sqlite import (
    MEMORY_PATH,
    PROVIDER,
    SqliteDatabase,
    SqliteSettings,
    clean_row,
    create_index_sql,
    create_pages_table_sql,
    create_table_sql,
    stored_timestamp,
)

NOTICE = re.compile(r'<p class="notice[^>]*>(.*?)</p>', re.S)


def sample_result(filename: str = "invoice.pdf") -> ExtractionResult:
    """A finished extraction, as ``extract_text`` would return it."""
    return ExtractionResult(
        filename=filename,
        kind="pdf",
        pages=(
            PageResult(
                page_number=1,
                text="ACME invoice 2026",
                method="ocr",
                confidence=96.5,
                duration_ms=110,
            ),
            PageResult(
                page_number=2, text="Total 1234.56 USD", method="ocr", duration_ms=90
            ),
        ),
        duration_ms=200,
        languages="eng",
        tesseract_version="5.4.0",
        size_bytes=4096,
    )


def notices(response) -> list[str]:
    """The ``notice`` paragraphs of a rendered page (message/error banners)."""
    return [
        " ".join(match.split())
        for match in NOTICE.findall(response.get_data(as_text=True))
    ]


def page_text(response) -> str:
    """A rendered page with runs of whitespace collapsed, so a sentence can be asserted."""
    return " ".join(response.get_data(as_text=True).split())


def save(store: SqliteDatabase, filename: str, *page_texts: str) -> int:
    """Store a one-row-per-page extraction and return its record id."""
    pages = tuple(
        PageResult(page_number=number, text=text, method="ocr")
        for number, text in enumerate(page_texts, start=1)
    )
    return store.save_extraction(
        ExtractionResult(
            filename=filename, kind="pdf", pages=pages, duration_ms=5, languages="eng"
        )
    )


@pytest.fixture()
def store(tmp_path):
    """A connected SQLite store in a throw-away file."""
    database = SqliteDatabase(SqliteSettings(path=str(tmp_path / "ocr.sqlite3")))
    database.connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_settings_default_to_a_memory_database():
    settings = SqliteSettings()

    assert settings.path == MEMORY_PATH
    assert settings.is_memory is True
    assert settings.table == "ocr_extractions"
    assert settings.pages_table == "ocr_extractions_pages"
    assert settings.to_public_dict()["database"] == MEMORY_PATH, "templates print this"


def test_settings_reject_an_empty_path_and_unsafe_names():
    with pytest.raises(InvalidDatabaseSettingsError, match="Enter the path"):
        SqliteSettings(path="  ")

    with pytest.raises(InvalidDatabaseSettingsError, match="valid SQLite name"):
        SqliteSettings.from_mapping({"table": "bad name"})

    with pytest.raises(InvalidDatabaseSettingsError, match="different name"):
        SqliteSettings(table="docs", pages_table="docs")


def test_settings_take_the_submitted_values_and_keep_the_defaults(tmp_path):
    base = SqliteSettings(path=str(tmp_path / "base.sqlite3"), table="base_docs")
    settings = SqliteSettings.from_mapping({"path": " ", "table": "ocr_docs"}, defaults=base)

    assert settings.path == base.path, "a blank field falls back to the default"
    assert settings.table == "ocr_docs"
    assert settings.pages_table == "ocr_docs_pages", "derived from the table name"

    with pytest.raises(InvalidDatabaseSettingsError, match="Timeout must be"):
        SqliteSettings.from_mapping({"timeout": "0"})


def test_backend_helpers_name_the_stores():
    assert normalize_backend("SQLite") == BACKEND_SQLITE
    assert normalize_backend("MYSQL") == BACKEND_MYSQL
    assert normalize_backend("nonsense") == "auto", "unknown values fall back safely"
    assert normalize_backend(None, BACKEND_SQLITE) == BACKEND_SQLITE

    assert backend_labels("auto").label == "MySQL", "auto asks MySQL first"
    assert backend_labels(BACKEND_SQLITE).server_term == "SQLite database"
    assert backend_labels(BACKEND_MYSQL).server_phrase == "a MySQL server"


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
def test_ddl_builds_the_same_tables_as_the_mysql_store():
    table = create_table_sql("ocr_docs")
    pages = create_pages_table_sql("ocr_docs_pages", "ocr_docs")
    indexes = create_index_sql("ocr_docs")

    for column in (
        "`id` INTEGER PRIMARY KEY AUTOINCREMENT",
        "`filename` VARCHAR(255) NOT NULL",
        "`uploaded_at` DATETIME NOT NULL",
        "`content` TEXT NOT NULL",
        "`content_sha256` CHAR(64) NULL",
        "`stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    ):
        assert column in table
    assert "`ocr_docs_pages`" in pages
    assert "FOREIGN KEY (`extraction_id`) REFERENCES `ocr_docs` (`id`) ON DELETE CASCADE" in pages
    assert len(indexes) == 3
    assert "`idx_ocr_docs_uploaded_at`" in indexes[0]
    assert "`idx_ocr_docs_sha256`" in indexes[2]

    with pytest.raises(InvalidDatabaseSettingsError, match="valid SQLite name"):
        create_table_sql("bad name")


# ---------------------------------------------------------------------------
# connecting
# ---------------------------------------------------------------------------
def test_connect_creates_the_file_tables_and_indexes(tmp_path):
    path = tmp_path / "nested" / "ocr.sqlite3"
    database = SqliteDatabase(SqliteSettings(path=str(path)))

    database.connect()

    assert path.is_file(), "the file, and the folder above it, are created"
    assert database.database_created is True
    assert database.tables_created is True
    assert database.server_version, "the sqlite version is reported"
    objects = database._fetchall(
        "SELECT `name` FROM `sqlite_master` "
        "WHERE `type` = 'index' AND `name` LIKE 'idx_%'"
    )
    assert {row["name"] for row in objects} == {
        "idx_ocr_extractions_uploaded_at",
        "idx_ocr_extractions_filename",
        "idx_ocr_extractions_sha256",
    }
    database.close()


def test_connect_again_is_a_no_op(store, tmp_path):
    again = SqliteDatabase(SqliteSettings(path=str(tmp_path / "ocr.sqlite3")))
    again.connect()

    assert again.database_created is False, "the file was already there"
    assert again.tables_created is False, "the tables were already there"
    again.close()


def test_a_tilde_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    database = SqliteDatabase(SqliteSettings(path="~/tilde.sqlite3"))

    database.connect()

    assert (tmp_path / "tilde.sqlite3").is_file()
    database.close()


def test_ensure_schema_recreates_a_dropped_table(store):
    store._execute("DROP TABLE ocr_extractions_pages")

    assert store.ensure_schema() is True
    assert store._table_exists("ocr_extractions_pages") is True
    assert store.ensure_schema() is False, "nothing left to create"


# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------
def test_save_extraction_stores_name_timestamp_content_and_pages(store):
    record_id = store.save_extraction(
        sample_result(), uploaded_at=datetime(2026, 7, 8, 9, 10, 11)
    )

    assert record_id == 1
    record = store.get_extraction(record_id)
    assert record["filename"] == "invoice.pdf"
    assert record["uploaded_at"] == "2026-07-08 09:10:11 UTC"
    assert record["content"] == sample_result().full_text()
    assert record["page_count"] == 2
    assert record["char_count"] == sample_result().char_count
    assert record["confidence"] == 96.5
    assert record["content_sha256"] == content_sha256(sample_result().full_text())
    assert [page["content"] for page in record["pages"]] == [
        "ACME invoice 2026",
        "Total 1234.56 USD",
    ]
    assert record["pages"][0]["confidence"] == 96.5


def test_save_extraction_defaults_the_timestamp_to_utc_now(store):
    store.save_extraction(sample_result())

    record = store.get_extraction(1)
    assert record["uploaded_at"].endswith(" UTC")
    stored = datetime.strptime(record["uploaded_at"][:19], "%Y-%m-%d %H:%M:%S")
    difference = datetime.now(timezone.utc) - stored.replace(tzinfo=timezone.utc)
    assert abs(difference.total_seconds()) < 30
    assert record["stored_at"].endswith(" UTC"), "the CURRENT_TIMESTAMP default too"


def test_save_extraction_rolls_the_whole_transaction_back(store):
    """A page that breaks the UNIQUE key must not leave a parent row behind."""
    duplicate_page = ExtractionResult(
        filename="twice.pdf",
        kind="pdf",
        pages=(
            PageResult(page_number=1, text="one", method="ocr"),
            PageResult(page_number=1, text="again", method="ocr"),
        ),
        duration_ms=1,
        languages="eng",
    )

    with pytest.raises(DatabaseWriteError, match="Saving the extraction failed"):
        store.save_extraction(duplicate_page)

    assert store.record_count() == 0, "nothing may be half written"
    assert store._fetchall("SELECT COUNT(*) AS `total` FROM ocr_extractions_pages")[0][
        "total"
    ] == 0


def test_save_extraction_requires_a_connection():
    with pytest.raises(DatabaseNotConfiguredError, match="No SQLite database is connected"):
        SqliteDatabase(SqliteSettings(path=MEMORY_PATH)).save_extraction(sample_result())


def test_delete_extraction_cascades_to_the_pages(store):
    record_id = store.save_extraction(sample_result())

    assert store.delete_extraction(record_id) is True
    assert store.record_count() == 0
    assert store._fetchall("SELECT COUNT(*) AS `total` FROM ocr_extractions_pages")[0][
        "total"
    ] == 0, "PRAGMA foreign_keys is on"
    assert store.delete_extraction(record_id) is False, "already gone"


def test_require_extraction_raises_a_not_found_error(store):
    with pytest.raises(DatabaseRecordNotFoundError, match="not stored in"):
        store.require_extraction(42)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def test_search_extractions_matches_the_name_the_text_and_the_record_number(store):
    save(store, "invoice.pdf", "ACME invoice")
    save(store, "letter.pdf", "Dear Sir")

    assert [row["id"] for row in store.search_extractions("acme")] == [1], "case-insensitive"
    assert [row["id"] for row in store.search_extractions("dear")] == [2]
    assert [row["id"] for row in store.search_extractions("2")] == [2], "a number is an id too"
    assert store.search_extractions("nothing like this") == []
    assert [row["id"] for row in store.search_extractions("")] == [2, 1], "blank lists all"


def test_search_extractions_narrows_to_one_scope(store):
    save(store, "invoice.pdf", "Dear Sir")

    assert [row["id"] for row in store.search_extractions("invoice", scope="content")] == []
    assert [row["id"] for row in store.search_extractions("invoice", scope="filename")] == [1]
    assert [row["id"] for row in store.search_extractions("invoice", scope="nonsense")] == [1]


def test_search_extractions_treats_typed_wildcards_literally(store):
    save(store, "report_100%.pdf", "alpha")
    save(store, "report-1000.pdf", "beta")

    assert [row["filename"] for row in store.search_extractions("report_100")] == [
        "report_100%.pdf"
    ], "the typed underscore and percent stay literal"
    assert [row["filename"] for row in store.search_extractions("report-1000")] == [
        "report-1000.pdf"
    ]


def test_recent_extractions_are_newest_first_and_never_select_the_document(store):
    save(store, "first.pdf", "one")
    save(store, "second.pdf", "two")

    rows = store.recent_extractions(limit=1)
    assert len(rows) == 1
    assert rows[0]["filename"] == "second.pdf"
    assert "content" not in rows[0], "the list only carries a preview"
    assert rows[0]["preview"] == "two"
    assert rows[0]["content_chars"] == 3

    assert len(store.recent_extractions(limit="nonsense")) == 2, "a silly limit falls back"
    assert len(store.recent_extractions(limit=10_000)) == 2


def test_count_extractions_counts_the_matches(store):
    save(store, "invoice.pdf", "ACME invoice")
    save(store, "letter.pdf", "Dear Sir")

    assert store.count_extractions() == 2, "no term, everything"
    assert store.count_extractions("acme") == 1, "the same criteria as the listing"
    assert store.count_extractions("dear", scope="content") == 1
    assert store.count_extractions("dear", scope="filename") == 0
    assert store.count_extractions("nothing like this") == 0


def test_search_extractions_pages_with_an_offset(store):
    save(store, "first.pdf", "one")
    save(store, "second.pdf", "two")
    save(store, "third.pdf", "three")

    assert [row["filename"] for row in store.search_extractions("", 2)] == [
        "third.pdf",
        "second.pdf",
    ], "page one, newest first"
    assert [row["filename"] for row in store.search_extractions("", 2, offset=2)] == [
        "first.pdf"
    ], "the second page continues where the first stopped"
    assert store.search_extractions("", 2, offset=10) == [], "past the end is empty"
    assert [
        row["filename"] for row in store.recent_extractions(2, offset="nonsense")
    ] == ["third.pdf", "second.pdf"], "a broken offset pages from the start"


def test_export_extractions_selects_the_whole_text(store):
    save(store, "first.pdf", "one", "two")

    rows = store.export_extractions()
    assert "content" in rows[0]
    assert "Page 2 of 2" in rows[0]["content"]
    assert rows[0]["content_sha256"] == content_sha256(rows[0]["content"])
    assert store.export_extractions("no such text") == []


def test_pages_for_extractions_reads_every_page_in_one_query(store):
    first = save(store, "first.pdf", "one", "two")
    second = save(store, "second.pdf", "three")

    pages = store.pages_for_extractions([first, second])
    assert [page["extraction_id"] for page in pages] == [first, first, second]
    assert [page["page_number"] for page in pages] == [1, 2, 1]
    assert pages[0]["content"] == "one"
    assert store.pages_for_extractions([]) == [], "no ids, no query"


def test_status_reports_the_file_and_what_connecting_created(store, tmp_path):
    status = store.status()

    assert status["provider"] == PROVIDER == "sqlite"
    assert status["connected"] is True
    assert status["connection_label"] == str(tmp_path / "ocr.sqlite3")
    assert status["settings"]["table"] == "ocr_extractions"
    assert status["settings"]["pages_table"] == "ocr_extractions_pages"
    assert status["database_created"] is True
    assert status["tables_created"] is True
    assert status["connected_at"].endswith(" UTC")
    assert "password" not in json.dumps(status)


def test_row_helper_words_timestamps_like_the_mysql_store():
    row = {
        "uploaded_at": "2026-01-02 03:04:05.678901",
        "stored_at": "2026-01-02 03:04:06",
        "content": "x",
        "confidence": None,
    }

    cleaned = clean_row(row)

    assert cleaned["uploaded_at"] == "2026-01-02 03:04:05 UTC"
    assert cleaned["stored_at"] == "2026-01-02 03:04:06 UTC"
    assert cleaned["content"] == "x"
    assert clean_row(None) == {}
    assert stored_timestamp(datetime(2026, 1, 2, 3, 4, 5, 678901)) == (
        "2026-01-02 03:04:05.678901"
    )


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------
def test_manager_connects_the_configured_sqlite_backend(tmp_path):
    path = tmp_path / "ocr.sqlite3"
    manager = DatabaseManager(
        sqlite_defaults=SqliteSettings(path=str(path)), backend=BACKEND_SQLITE
    )

    database = manager.connect({"backend": ""})  # the form said nothing

    assert database.provider == PROVIDER
    assert manager.backend == BACKEND_SQLITE
    assert manager.labels.label == "SQLite"
    assert path.is_file(), "connecting creates the file"
    assert manager.is_connected is True


def test_manager_remembers_the_sqlite_path_for_the_next_form(tmp_path):
    manager = DatabaseManager(backend=BACKEND_SQLITE)
    path = str(tmp_path / "custom.sqlite3")

    manager.connect_sqlite({"path": path})

    assert manager.suggest_sqlite_settings().path == path
    assert manager.status()["backends"][BACKEND_SQLITE]["path"] == path


def test_manager_still_asks_mysql_when_the_form_requests_it(monkeypatch, tmp_path):
    monkeypatch.setattr("app.database.pymysql", None)
    manager = DatabaseManager(
        backend=BACKEND_SQLITE, sqlite_defaults=SqliteSettings(path=str(tmp_path / "x.sqlite3"))
    )

    with pytest.raises(DatabaseUnavailableError, match="not installed"):
        manager.connect({"backend": BACKEND_MYSQL, "host": "localhost", "user": "root"})

    assert manager.is_connected is False


def test_auto_connect_falls_back_to_sqlite_when_mysql_is_not_there(monkeypatch, tmp_path):
    monkeypatch.setattr("app.database.pymysql", None)
    path = tmp_path / "fallback.sqlite3"
    manager = DatabaseManager(sqlite_defaults=SqliteSettings(path=str(path)))

    database = manager.auto_connect()

    assert database.provider == PROVIDER, "no MySQL driver, so the file is used"
    assert path.is_file()


def test_auto_connect_reports_the_failure_when_mysql_is_pinned(monkeypatch, tmp_path):
    monkeypatch.setattr("app.database.pymysql", None)
    manager = DatabaseManager(
        backend=BACKEND_MYSQL, sqlite_defaults=SqliteSettings(path=str(tmp_path / "x.sqlite3"))
    )

    with pytest.raises(DatabaseUnavailableError, match="not installed"):
        manager.auto_connect()


def test_auto_connect_uses_sqlite_directly_when_it_is_configured(tmp_path):
    path = tmp_path / "direct.sqlite3"
    manager = DatabaseManager(
        backend=BACKEND_SQLITE, sqlite_defaults=SqliteSettings(path=str(path))
    )

    assert manager.auto_connect().provider == PROVIDER
    assert path.is_file()


def test_status_without_a_connection_offers_both_backends(client):
    status = client.get("/api/database").get_json()["database"]

    assert status["provider"] == "mysql", "auto names MySQL until something is connected"
    assert status["backend"] == "auto"
    assert set(status["backends"]) == {"mysql", "sqlite"}
    assert status["backends"]["sqlite"]["available"] is True
    assert status["backends"]["sqlite"]["driver"]["name"] == "sqlite3"
    assert status["backends"]["sqlite"]["path"].endswith("ocr_records.sqlite3")
    assert status["settings"]["table"] == "ocr_extractions"


# ---------------------------------------------------------------------------
# the Database page, the upload form and the JSON API
# ---------------------------------------------------------------------------
def test_database_page_offers_the_sqlite_form(client):
    body = client.get("/database").get_data(as_text=True)

    assert "Use a local SQLite file" in body
    assert 'name="backend" value="sqlite"' in body
    assert 'name="path"' in body
    assert "ocr_records.sqlite3" in body, "the default path is pre-filled"
    assert "Connect SQLite file" in body


def test_the_store_panels_fold_away_and_reopen_for_the_store_that_was_used(
    make_client, tmp_path
):
    """The two folded sections; the one a request was about unfolds itself."""
    path = str(tmp_path / "ocr.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)

    fresh = client.get("/database").get_data(as_text=True)
    assert fresh.count("<details") == 2, "the SQLite form and the connection status"
    assert not unfolded(fresh), "a fresh visit opens none of them"
    assert 'id="db-host"' in fresh, "the MySQL form is on screen, never folded away"

    failed = client.post(
        "/database/connect", data={"backend": BACKEND_SQLITE, "path": str(tmp_path)}
    )
    assert failed.status_code == 503
    assert "Use a local SQLite file" in unfolded(failed.get_data(as_text=True))

    connected = client.post(
        "/database/connect", data={"backend": BACKEND_SQLITE, "path": path}
    )
    assert connected.status_code == 200
    assert "Connection status" in unfolded(
        connected.get_data(as_text=True)
    ), "the state that just changed is unfolded, not folded away"


def unfolded(body: str) -> str:
    """The ``<details>`` panels rendered with ``open``, joined into one string."""
    return "".join(
        panel.group(0)
        for panel in re.finditer(r"<details[^>]*>.*?</details>", body, re.DOTALL)
        if re.search(r"<details[^>]*\bopen\b", panel.group(0))
    )


def test_the_upload_page_names_the_configured_store(make_client, tmp_path, text_pdf_factory):
    path = str(tmp_path / "ocr.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)

    body = client.get("/").get_data(as_text=True)
    assert "Database storage (SQLite)" in body
    assert "Save the reviewed data to SQLite" not in body, "saving is decided on the review page"

    client.post("/database/connect", data={"backend": BACKEND_SQLITE, "path": path})
    connected = client.get("/").get_data(as_text=True)
    assert path in connected, "the configured file is named before anything is stored"

    review = client.post(
        "/upload",
        data={"file": (io.BytesIO(text_pdf_factory("Total: 1.00 EUR")), "doc.pdf")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)
    assert "Save the reviewed data to SQLite" in review, "only offered while connected"
    assert path in review


def test_connect_sqlite_then_store_search_export_and_delete(
    make_client, tmp_path, text_pdf_factory, review_save
):
    path = str(tmp_path / "ocr.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)

    connected = client.post(
        "/database/connect", data={"backend": BACKEND_SQLITE, "path": path}
    )
    assert connected.status_code == 200
    assert any("Created the SQLite file" in notice for notice in notices(connected))

    upload = client.post(
        "/upload",
        data={
            "file": (
                io.BytesIO(text_pdf_factory("ACME invoice 2026\nTotal: 42.00 EUR")),
                "invoice.pdf",
            )
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200

    saved = review_save(client, upload, supplier_0="ACME GmbH")
    assert saved.status_code == 200
    assert "Stored in SQLite as" in saved.get_data(as_text=True)

    records = client.get("/database/records?q=invoice").get_data(as_text=True)
    assert "invoice.pdf" in records
    assert "1 match" in records
    assert "ACME GmbH" in records, "the records table shows the reviewed fields"

    detail = client.get("/database/records/1").get_data(as_text=True)
    assert "Stored in SQLite" in detail
    assert "ACME invoice 2026" in detail
    assert "ACME GmbH" in detail

    export = client.get("/database/records/export.xlsx")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.data)) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()

    deleted = client.post("/database/records/1/delete")
    assert deleted.status_code == 303
    assert "Nothing stored yet" in client.get("/database/records").get_data(as_text=True)



def test_api_connect_sqlite_creates_the_file_and_reports_it(make_client, tmp_path):
    path = tmp_path / "api.sqlite3"
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=str(path))

    response = client.post(
        "/api/database/connect", json={"backend": BACKEND_SQLITE, "path": str(path)}
    )
    payload = response.get_json()["database"]

    assert response.status_code == 200
    assert payload["provider"] == PROVIDER
    assert payload["connected"] is True
    assert payload["database_created"] is True
    assert payload["tables_created"] is True
    assert "Created the SQLite file" in payload["message"]
    assert payload["record_count"] == 0
    assert path.is_file()

    health = client.get("/api/health").get_json()["database"]
    assert health["provider"] == PROVIDER
    assert health["sqlite_available"] is True
    assert health["connected"] is True

    assert client.post("/api/database/disconnect").get_json()["was_connected"] is True
    assert client.get("/api/database/records").status_code == 400


def test_api_connect_reports_bad_sqlite_settings(make_client, tmp_path):
    client = make_client(SQLITE_PATH=str(tmp_path / "x.sqlite3"))

    response = client.post(
        "/api/database/connect", json={"backend": BACKEND_SQLITE, "table": "bad name"}
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_database_settings"
    assert "valid SQLite name" in error["message"]


def test_sqlite_is_the_way_out_when_the_mysql_driver_is_missing(
    make_client, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.database.pymysql", None)
    path = str(tmp_path / "fallback.sqlite3")
    client = make_client(SQLITE_PATH=path)

    page = client.get("/database").get_data(as_text=True)
    assert "The MySQL driver is not installed" in page
    assert "Use a local SQLite file" in page

    failed = client.post("/database/connect", data={"host": "localhost", "user": "root"})
    assert failed.status_code == 503
    assert "Use a local SQLite file" in failed.get_data(as_text=True)

    connected = client.post(
        "/database/connect", data={"backend": BACKEND_SQLITE, "path": path}
    )
    assert connected.status_code == 200
    assert any("Created the SQLite file" in notice for notice in notices(connected))


def test_records_view_lists_every_stored_record(make_client, tmp_path):
    """A store that is not empty lists all of it - twelve rows, one page."""
    path = str(tmp_path / "all.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)
    client.post("/database/connect", data={"backend": BACKEND_SQLITE, "path": path})
    manager = client.application.extensions["ocr_database"]
    for number in range(1, 13):
        manager.save_extraction(sample_result(f"doc-{number:02d}.pdf"))

    body = page_text(client.get("/database/records"))

    assert "doc-12.pdf" in body and "doc-01.pdf" in body, "every stored row, newest first"
    assert "All 12 stored records." in body
    assert "records-pagination" not in body, "everything fits - no page buttons"

    empty = page_text(client.get("/database/records?q=doc-99"))
    assert "Nothing stored yet" not in empty
    assert "No stored record matches that search" in empty


def test_records_row_keeps_the_full_cell_text_when_it_is_truncated(make_client, tmp_path):
    """A row is one line tall: a long cell is cut off, so its full text travels as
    the cell's ``title`` (the hover tooltip), and the two buttons sit in their own
    wrapper inside the cell - which is what lets ``vertical-align`` centre them on
    that line instead of ``display: flex`` turning the ``<td>`` into a block."""
    path = str(tmp_path / "titles.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)
    client.post("/database/connect", data={"backend": BACKEND_SQLITE, "path": path})
    manager = client.application.extensions["ocr_database"]
    manager.save_extraction(
        sample_result("Apr_15_2_RMAVZ.pdf"),
        fields=DocumentFields.from_mapping(
            {
                "supplier": "PRYCE GASES INCORPORATED",
                "invoice_number": "PGI-25-0155",
                "document_date": "2026-03-24",
                "total_amount": "623.00",
            }
        ),
    )

    body = page_text(client.get("/database/records"))

    summary = "PRYCE GASES INCORPORATED \u00b7 PGI-25-0155 \u00b7 2026-03-24 \u00b7 623.00"
    assert f'class="cell-fields" title="{summary}"' in body, "the summary on hover"
    assert (
        'class="cell-preview" title="----- Page 1 of 2 -----' in body
    ), "the snippet on hover - and its line breaks collapsed to spaces"
    assert 'title="Apr_15_2_RMAVZ.pdf"' in body, "the whole file name on hover"
    assert (
        '<td class="cell-actions"> <div class="cell-actions-inner">' in body
    ), "the buttons are wrapped inside the cell, not the cell itself"
    assert ".txt</a>" in body and "Delete</button>" in body


def test_records_view_pages_rows_with_working_navigation(make_client, tmp_path):
    path = str(tmp_path / "paged.sqlite3")
    client = make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=path)
    client.post("/database/connect", data={"backend": BACKEND_SQLITE, "path": path})
    manager = client.application.extensions["ocr_database"]
    for number in range(1, 13):
        manager.save_extraction(sample_result(f"doc-{number:02d}.pdf"))

    first = page_text(client.get("/database/records?limit=10"))
    assert "doc-12.pdf" in first and "doc-03.pdf" in first, "the ten newest rows"
    assert "doc-02.pdf" not in first, "the eleventh row belongs to page 2"
    assert "Showing rows 1-10 of 12 stored records (page 1 of 2)." in first
    assert 'href="/database/records?scope=all&amp;limit=10&amp;page=2"' in first, "Next links on"

    last = page_text(client.get("/database/records?limit=10&page=2"))
    assert "doc-02.pdf" in last and "doc-01.pdf" in last, "the remainder is the last page"
    assert "doc-03.pdf" not in last
    assert "Showing rows 11-12 of 12 stored records (page 2 of 2)." in last
    assert 'aria-disabled="true">Next &raquo;' in last, "nothing after the last page"

    beyond = page_text(client.get("/database/records?limit=10&page=99"))
    assert "doc-01.pdf" in beyond and "page 2 of 2" in beyond, "?page= lands on the last page"


def test_records_view_without_a_connection_asks_for_the_sqlite_store(make_client, tmp_path):
    client = make_client(
        DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=str(tmp_path / "x.sqlite3")
    )

    body = client.get("/database/records").get_data(as_text=True)

    assert "Nothing is connected yet, so no extraction is stored" in body
    assert '<a href="/database">connect to a SQLite database</a>' in body
    assert '<a href="/database">SQLite storage</a>' in body, "the way to connect is linked"
    assert "Nothing is connected yet, so extractions are not being stored" in body, (
        "the empty table points at the SQLite panel - no form sits above the table"
    )


# ---------------------------------------------------------------------------
# the shared fixtures (tests/conftest.py): one real file per test, no server
# ---------------------------------------------------------------------------
def test_sqlite_client_fixture_uploads_into_the_tmp_path_file(
    sqlite_client, sqlite_path, text_pdf_factory, review_save
):
    """The fixture's file is a real database: read the row back with plain sqlite3."""
    assert sqlite_path.is_file(), "connecting created the database file"

    upload = sqlite_client.post(
        "/upload",
        data={
            "file": (
                io.BytesIO(text_pdf_factory("Fixture invoice 2026 total 42")),
                "fixture.pdf",
            )
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200

    saved = review_save(sqlite_client, upload)
    assert saved.status_code == 200
    assert "Stored in SQLite as" in saved.get_data(as_text=True)
    stored_view = sqlite_client.get("/database/records").get_data(as_text=True)
    assert "fixture.pdf" in stored_view

    # Straight out of the file, with no application code in the way.
    with sqlite3.connect(str(sqlite_path)) as connection:
        stored = connection.execute(
            "SELECT `filename`, `content` FROM `ocr_extractions`"
        ).fetchall()
    assert len(stored) == 1
    assert stored[0][0] == "fixture.pdf"
    assert "Fixture invoice 2026 total 42" in stored[0][1]



def test_sqlite_store_fixture_is_the_live_store_in_the_tmp_path_file(
    sqlite_store, sqlite_path
):
    """The fixture for direct assertions: same file, no HTTP round trip."""
    assert sqlite_store.provider == PROVIDER
    assert sqlite_store.settings.path == str(sqlite_path)

    record_id = sqlite_store.save_extraction(sample_result("direct.pdf"))

    assert record_id == 1
    assert sqlite_store.record_count() == 1
    with sqlite3.connect(str(sqlite_path)) as connection:
        rows = connection.execute("SELECT `filename` FROM `ocr_extractions`").fetchall()
    assert rows == [("direct.pdf",)]


# ---------------------------------------------------------------------------
# the ``save_to_db`` field pair the review form sends (hidden 0 + ticked box 1)
# ---------------------------------------------------------------------------
def test_the_review_form_offers_a_ticked_save_checkbox_and_its_hidden_twin(
    sqlite_client, text_pdf_factory
):
    """The two fields the tests below post are the two fields the page renders."""
    review = sqlite_client.post(
        "/upload",
        data={"file": (io.BytesIO(text_pdf_factory("Any invoice 2026")), "doc.pdf")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)

    assert '<input type="hidden" name="save_to_db" value="0">' in review
    assert '<input type="checkbox" name="save_to_db" value="1" checked' in review


def test_reviewing_from_the_form_stores_the_extraction(
    sqlite_client, sqlite_path, text_pdf_factory, review_save
):
    """The browser posts the hidden ``0`` **and** the ticked checkbox ``1``.

    Reading only the first value found the hidden ``0`` and stored nothing, so this
    is the regression test for the checkbox that never saved.
    """
    upload = sqlite_client.post(
        "/upload",
        data={
            "save_to_db": ["0", "1"],
            "file": (io.BytesIO(text_pdf_factory("Form upload 2026")), "form.pdf"),
        },
        content_type="multipart/form-data",
    )

    assert upload.status_code == 200
    saved = review_save(sqlite_client, upload, save_to_db=["0", "1"])
    assert saved.status_code == 200
    assert "Stored in SQLite as" in saved.get_data(as_text=True), "a ticked box saves"
    with sqlite3.connect(str(sqlite_path)) as connection:
        rows = connection.execute("SELECT `filename` FROM `ocr_extractions`").fetchall()
    assert rows == [("form.pdf",)]


def test_unticking_the_save_checkbox_keeps_the_reviewed_document_out_of_the_store(
    sqlite_client, sqlite_store, text_pdf_factory, review_save
):
    """An unticked box submits only the hidden ``0`` - and that means "do not save"."""
    upload = sqlite_client.post(
        "/upload",
        data={
            "file": (io.BytesIO(text_pdf_factory("Opted out 2026")), "opted-out.pdf"),
        },
        content_type="multipart/form-data",
    )

    saved = review_save(sqlite_client, upload, save_to_db="0")

    assert saved.status_code == 200
    assert "Stored in SQLite" not in saved.get_data(as_text=True)
    assert "storing was switched off" in saved.get_data(as_text=True)
    assert sqlite_store.record_count() == 0, "the text is extracted, but not stored"



# ---------------------------------------------------------------------------
# start-up
# ---------------------------------------------------------------------------
def _app_for_startup(path, tesseract_command, **overrides):
    """An application that connects its store during ``create_app``."""
    config = {
        "TESTING": True,
        "SECRET_KEY": "testing",
        "TESSERACT_CMD": tesseract_command,
        "DATABASE_AUTO_CONNECT": True,
        "SQLITE_PATH": str(path),
    }
    config.update(overrides)
    return create_app(config)


def test_start_up_opens_the_configured_sqlite_file(tmp_path, tesseract_command):
    path = tmp_path / "auto.sqlite3"
    app = _app_for_startup(path, tesseract_command, DATABASE_BACKEND=BACKEND_SQLITE)

    manager = app.extensions["ocr_database"]
    assert manager.is_connected is True
    assert manager.status()["provider"] == PROVIDER
    assert path.is_file()
    app.extensions["ocr_result_store"].clear()


def test_start_up_falls_back_to_sqlite_when_mysql_is_unreachable(
    tmp_path, monkeypatch, tesseract_command
):
    monkeypatch.setattr("app.database.pymysql", None)
    path = tmp_path / "fallback.sqlite3"

    app = _app_for_startup(path, tesseract_command)  # DATABASE_BACKEND defaults to auto

    manager = app.extensions["ocr_database"]
    assert manager.status()["provider"] == PROVIDER, "auto keeps a working store"
    assert path.is_file()
    app.extensions["ocr_result_store"].clear()
