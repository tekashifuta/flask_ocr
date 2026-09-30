"""Tests for the MySQL storage layer, the connection UI and its JSON API.

No MySQL server is required: :class:`FakeConnection` implements the small slice of
the PyMySQL surface the persistence layer uses (``cursor``/``execute``,
``commit``/``rollback``, ``ping``, ``select_db``) and records every statement, so
the SQL, the schema creation and the row mapping are all observable.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

import pymysql
import pytest

from app.database import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_TABLE,
    MAX_LIST_LIMIT,
    MAX_RECORD_PAGE,
    MAX_SEARCH_CHARS,
    DatabaseManager,
    MySqlDatabase,
    MySqlSettings,
    clamp_record_limit,
    clamp_record_offset,
    clamp_record_page,
    clean_search_term,
    content_sha256,
    create_database_sql,
    create_pages_table_sql,
    create_table_sql,
    like_pattern,
    normalize_search_scope,
)
from app.exceptions import (
    DatabaseNotConfiguredError,
    DatabaseRecordNotFoundError,
    DatabaseUnavailableError,
    DatabaseWriteError,
    InvalidDatabaseSettingsError,
)
from app.excel import XLSX_MIMETYPE
from app.fields import FIELD_ORDER
from app.ocr import ExtractionResult, PageResult


BACKTICKED = re.compile(r"`([A-Za-z0-9_]+)`")
INSERT_COLUMNS = re.compile(r"\(([^)]*)\)\s*values", re.IGNORECASE)
LIKE_COLUMN = re.compile(r"`([A-Za-z0-9_]+)` like %s", re.IGNORECASE)
#: Column definition of one part of a ``CREATE TABLE`` body: a quoted name followed
#: by its type.  ``PRIMARY KEY (...)``, ``KEY ...``, ``UNIQUE KEY ...`` and
#: ``CONSTRAINT ...`` do not start with a quoted name, which is what keeps them out.
DDL_COLUMN = re.compile(r"^\s*`([A-Za-z0-9_]+)`\s+[A-Za-z]")


def ddl_columns(statement: str) -> set[str]:
    """The column names a ``CREATE TABLE`` declares."""
    body = statement[statement.index("(") + 1 : statement.rindex(")")]
    return {
        match.group(1)
        for match in (DDL_COLUMN.match(part) for part in body.split(","))
        if match
    }




def columns_of(statement: str) -> list[str]:
    """Column names of an ``INSERT INTO t (...) VALUES (...)`` statement."""
    match = INSERT_COLUMNS.search(statement)
    return [name.strip().strip("`") for name in match.group(1).split(",")]


def first_backticked(statement: str) -> str:
    """First quoted identifier - the table name in every statement we generate."""
    return BACKTICKED.search(statement).group(1)


def like_matches(value: str, pattern: str) -> bool:
    """Case-insensitive match of a MySQL ``LIKE`` pattern, ``!`` escapes included.

    ``!%``/``!_`` are literals, a bare ``%``/``_`` is a wildcard - exactly what the
    escaping in ``app.database.like_pattern`` is there to control.
    """
    regex: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "!" and index + 1 < len(pattern):
            regex.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        regex.append({"%": ".*", "_": "."}.get(char, re.escape(char)))
        index += 1
    return re.search("".join(regex), value, re.IGNORECASE | re.DOTALL) is not None


class FakeCursor:
    """Cursor that defers to its connection's tiny SQL interpreter."""

    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self.lastrowid = 0
        self.rowcount = -1
        self._rows: list[dict] = []
        self.closed = False

    def execute(self, sql, params=None) -> None:
        self._rows, self.lastrowid, self.rowcount = self._connection.handle(sql, params)

    def executemany(self, sql, seq_of_params) -> None:
        self._rows, self.lastrowid, self.rowcount = self._connection.handle(
            sql, list(seq_of_params)
        )

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Enough of a PyMySQL connection to exercise the storage layer offline."""

    def __init__(
        self,
        *,
        schemas: tuple[str, ...] = (),
        tables: tuple[str, ...] = (),
        records: list[dict] | None = None,
        server_version: str = "8.0.36-fake",
        pages_table: str = f"{DEFAULT_TABLE}_pages",
    ) -> None:
        self.schemas = set(schemas)
        self.tables = set(tables)
        #: Table -> its column names, filled in by ``CREATE TABLE`` (what
        #: ``information_schema.COLUMNS`` answers with on a real server).
        self.columns: dict[str, set[str]] = {}

        self.records = list(records or [])
        self.server_version = server_version
        self.pages_table = pages_table
        self.page_rows: list[tuple] = []
        self.statements: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.current_database: str | None = None
        self.commits = 0
        self.rollbacks = 0
        self.pings = 0
        self.closed = False
        self.next_id = len(self.records)
        #: Substring of a statement that should fail (simulates a broken server).
        self.fail_on: str | None = None
        self.kwargs: dict = {}

    # -- connection API --------------------------------------------------
    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def begin(self) -> None:
        pass

    def ping(self, reconnect: bool = True) -> None:
        self.pings += 1

    def select_db(self, name: str) -> None:
        self.current_database = name

    def close(self) -> None:
        self.closed = True

    # -- the "SQL engine" ------------------------------------------------
    def handle(self, sql, params=None):
        statement = " ".join(str(sql).split())
        self.statements.append(statement)
        self.calls.append((statement, params))
        lowered = statement.lower()

        if self.fail_on and self.fail_on in lowered:
            raise pymysql.err.OperationalError(2003, f"simulated failure: {self.fail_on}")

        if "information_schema" in lowered and "schemata" in lowered:
            found = params[0] in self.schemas
            return ([{"SCHEMA_NAME": params[0]}] if found else []), -1, int(found)
        if lowered.startswith("create database"):
            self.schemas.add(first_backticked(statement))
            return [], -1, 1
        if lowered.startswith("create table"):
            table = first_backticked(statement)
            if table not in self.tables:
                # ``IF NOT EXISTS``: an existing table keeps its columns, which is
                # precisely why the structured fields need their own ALTER TABLE.
                self.columns[table] = ddl_columns(statement)
            self.tables.add(table)
            return [], -1, 0

        if "information_schema" in lowered and "column_name" in lowered:
            # ``_table_columns``: which columns the (possibly old) table has.
            names = self.columns.get(params[1], set())
            return ([{"COLUMN_NAME": name} for name in sorted(names)], -1, len(names))
        if lowered.startswith("alter table"):
            name = first_backticked(statement)
            added = re.search(r"add column `([A-Za-z0-9_]+)`", lowered)
            if added:
                self.columns.setdefault(name, set()).add(added.group(1))
            return [], -1, 0
        if "information_schema" in lowered and "table_name" in lowered:
            found = params[0] in self.schemas and params[1] in self.tables
            return ([{"TABLE_NAME": params[1]}] if found else []), -1, int(found)

        if "select database()" in lowered:
            return [{"current_database": self.current_database}], -1, 1
        if "select version()" in lowered:
            return [{"server_version": self.server_version}], -1, 1
        if lowered.startswith("select count(*)"):
            # ``count_extractions`` reuses the search clause, so the count only
            # matches the rows the listing itself would return.
            matched = self._search(lowered, params) if " like %s" in lowered else self.records
            return [{"total": len(matched)}], -1, 1
        if lowered.startswith("insert into"):
            table = first_backticked(statement)
            rows = params if isinstance(params, list) else [params]
            if table == self.pages_table:
                names = columns_of(statement)
                self.page_rows.extend(dict(zip(names, row)) for row in rows)
                return [], 0, len(rows)
            for row in rows:
                self.next_id += 1
                record = dict(zip(columns_of(statement), row))
                record["id"] = self.next_id
                self.records.append(record)
            return [], self.next_id, len(rows)
        if lowered.startswith("delete from"):
            before = len(self.records)
            self.records = [r for r in self.records if r.get("id") != params[0]]
            return [], -1, before - len(self.records)
        if lowered.startswith("select * from"):
            return [r for r in self.records if r.get("id") == params[0]], -1, 1
        if "`extraction_id` in (" in lowered:
            # The Excel export reads the pages of several records in one query.
            identifiers = {int(value) for value in params}
            pages = [row for row in self.page_rows if row["extraction_id"] in identifiers]
            pages.sort(key=lambda row: (row["extraction_id"], row["page_number"]))
            wanted = self._wanted_columns(statement)
            return [{name: row[name] for name in wanted} for row in pages], -1, len(pages)
        if "`page_number`" in lowered and "order by" in lowered:
            wanted = self._wanted_columns(statement)
            pages = [row for row in self.page_rows if row["extraction_id"] == params[0]]
            pages.sort(key=lambda row: row["page_number"])
            return [{name: row[name] for name in wanted} for row in pages], -1, len(pages)
        if lowered.startswith("select"):
            limit = int(params[-1]) if params else len(self.records)
            offset = 0
            if " offset %s" in lowered:
                # Paging: ``LIMIT %s OFFSET %s`` binds both, offset last.
                limit, offset = int(params[-2]), int(params[-1])
            rows = self._newest_first(
                self._search(lowered, params) if " like %s" in lowered else self.records
            )[offset : offset + limit]
            if "left(`content`" in lowered:
                preview_chars = int(params[0])
                rows = [
                    dict(
                        row,
                        preview=str(row.get("content", ""))[:preview_chars],
                        content_chars=len(str(row.get("content", ""))),
                    )
                    for row in rows
                ]
            return rows, -1, len(rows)
        return [], -1, 0

    def _newest_first(self, records: list[dict]) -> list[dict]:
        """The ``ORDER BY uploaded_at DESC, id DESC`` the listing queries ask for."""
        return sorted(
            records,
            key=lambda row: (str(row.get("uploaded_at")), row.get("id") or 0),
            reverse=True,
        )

    @staticmethod
    def _wanted_columns(statement: str) -> list[str]:
        """The column names of a ``SELECT a, b, ... FROM t`` statement."""
        selected = re.search(r"select (.*?) from", statement, re.IGNORECASE).group(1)
        return [name.strip().strip("`") for name in selected.split(",")]

    def _search(self, statement: str, params) -> list[dict]:
        """The tiny ``LIKE`` engine behind ``search_extractions``.

        The bound parameters are ``(<preview chars>?, <one pattern per LIKE>, id,
        limit)`` - so the columns of the statement line up with the patterns, and the
        value after them is the record-id shortcut (``None`` for a non-numeric term).
        The export selects the whole ``content`` column, i.e. no preview length is
        bound, which shifts everything by one.
        """
        columns = LIKE_COLUMN.findall(statement)
        start = 1 if "left(`content`" in statement.lower() else 0
        patterns = list(params[start : start + len(columns)])
        identifier = params[start + len(columns)]
        return [
            row
            for row in self.records
            if (identifier is not None and row.get("id") == identifier)
            or any(
                like_matches(str(row.get(column) or ""), pattern)
                for column, pattern in zip(columns, patterns)
            )
        ]


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
            PageResult(page_number=2, text="Total 1234.56 USD", method="ocr", duration_ms=90),
        ),
        duration_ms=200,
        languages="eng",
        tesseract_version="5.4.0",
        size_bytes=4096,
    )


def stored_row(record_id: int = 1, **overrides) -> dict:
    """A row as ``recent_extractions`` receives it from a ``DictCursor``."""
    row = {
        "id": record_id,
        "filename": "archived.pdf",
        "uploaded_at": datetime(2026, 1, 2, 3, 4, 5),
        "kind": "pdf",
        "page_count": 2,
        "char_count": 40,
        "word_count": 7,
        "confidence": Decimal("93.25"),
        "duration_ms": 250,
        "size_bytes": 2048,
        "ocr_language": "eng",
        "engine_version": "5.4.0",
        "stored_at": datetime(2026, 1, 2, 3, 4, 6),
        "content": "page one\npage two",
        "content_sha256": "0" * 64,
    }
    row.update(overrides)
    return row


def stored_page(extraction_id: int = 1, page_number: int = 1, **overrides) -> dict:
    """A page row as the export query receives it from a ``DictCursor``."""
    row = {
        "extraction_id": extraction_id,
        "page_number": page_number,
        "method": "ocr",
        "content": f"page {page_number} text",
        "char_count": 12,
        "word_count": 3,
        "confidence": Decimal("96.50"),
        "duration_ms": 110,
    }
    row.update(overrides)
    return row


@pytest.fixture()
def fake_mysql(monkeypatch):
    """Patch ``pymysql.connect``; the list of created connections is returned.

    Reconnecting models the *same* server: schemas, tables and stored rows are
    carried over, exactly like a real MySQL instance would keep them.
    """
    connections: list[FakeConnection] = []

    def _connect(**kwargs):
        connection = FakeConnection()
        if connections:
            previous = connections[-1]
            connection.schemas |= previous.schemas
            connection.tables |= previous.tables
            # The same columns too: reconnecting must not look like a fresh table,
            # or every connect would try to add the structured field columns again.
            connection.columns = {
                table: set(columns) for table, columns in previous.columns.items()
            }
            connection.records = list(previous.records)
            connection.page_rows = list(previous.page_rows)
            connection.next_id = previous.next_id

        connection.kwargs = kwargs
        connections.append(connection)
        return connection

    monkeypatch.setattr("app.database.pymysql.connect", _connect)
    return connections


def connect_database(fake_mysql, **settings) -> tuple[MySqlDatabase, FakeConnection]:
    """Connect a database through the fake driver and return both objects."""
    database = MySqlDatabase(MySqlSettings(**settings))
    database.connect()
    return database, fake_mysql[-1]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_settings_use_the_submitted_values_and_never_expose_the_password():
    settings = MySqlSettings.from_mapping(
        {
            "host": "db.example.com",
            "port": "3307",
            "user": "ocr",
            "password": "s3cret",
            "database": "ocr_store",
            "table": "documents",
        }
    )

    assert settings.port == 3307
    assert settings.database == "ocr_store"
    assert settings.table == "documents"
    # The pages table is derived from the - possibly renamed - parent table.
    assert settings.pages_table == "documents_pages"

    public = settings.to_public_dict()
    assert public["has_password"] is True
    assert "password" not in public
    assert "s3cret" not in json.dumps(public)
    assert settings.to_storage_dict()["password"] == "s3cret"


def test_settings_fall_back_to_defaults_for_blank_optional_fields():
    defaults = MySqlSettings(host="db.internal", user="ocr", password="stored")

    settings = MySqlSettings.from_mapping(
        {"host": "db.internal", "user": "ocr", "port": "", "database": "", "table": ""},
        defaults=defaults,
    )

    assert settings.port == 3306
    assert settings.database == "flask_ocr"
    assert settings.table == DEFAULT_TABLE
    assert settings.password == "stored", "a blank password must reuse the stored one"


def test_settings_require_a_host_and_a_user():
    with pytest.raises(InvalidDatabaseSettingsError, match="host name"):
        MySqlSettings.from_mapping({"host": "  ", "user": "root"})
    with pytest.raises(InvalidDatabaseSettingsError, match="user name"):
        MySqlSettings.from_mapping({"host": "localhost", "user": ""})


@pytest.mark.parametrize("port", ["not-a-number", "0", "70000"])
def test_settings_reject_an_unusable_port(port):
    with pytest.raises(InvalidDatabaseSettingsError, match="Port"):
        MySqlSettings.from_mapping({"host": "localhost", "user": "root", "port": port})


@pytest.mark.parametrize(
    "name", ["rm -rf", "extractions;DROP TABLE users", "a" * 65, "with space"]
)
def test_settings_reject_unsafe_identifiers(name):
    for field in ("database", "table"):
        with pytest.raises(InvalidDatabaseSettingsError, match="valid MySQL name"):
            MySqlSettings.from_mapping({"host": "localhost", "user": "root", field: name})


def test_settings_reject_identical_table_names():
    with pytest.raises(InvalidDatabaseSettingsError, match="different name"):
        MySqlSettings.from_mapping(
            {"host": "localhost", "user": "root", "table": "docs", "pages_table": "docs"}
        )


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
def test_database_ddl_is_idempotent_and_uses_the_requested_charset():
    statement = create_database_sql("ocr_store", "utf8mb4")

    assert statement == "CREATE DATABASE IF NOT EXISTS `ocr_store` CHARACTER SET utf8mb4"


def test_extractions_ddl_stores_name_date_and_content():
    statement = create_table_sql("documents")

    # The three required columns, plus the statistics the UI shows.
    for column in ("`filename`", "`uploaded_at`", "`content`", "`content_sha256`"):
        assert column in statement
    assert "CREATE TABLE IF NOT EXISTS `documents`" in statement
    assert "ENGINE=InnoDB" in statement
    assert "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT" in statement


def test_pages_ddl_cascades_from_the_extractions_table():
    statement = create_pages_table_sql("documents_pages", "documents")

    assert "CREATE TABLE IF NOT EXISTS `documents_pages`" in statement
    assert "REFERENCES `documents` (`id`) ON DELETE CASCADE" in statement
    assert "UNIQUE KEY `uq_documents_pages_page`" in statement


def test_ddl_keeps_every_identifier_within_mysql_limits():
    """A 64 character table name may not push index/constraint names over 64."""
    table = "t" * 64
    pages_table = "p" * 64

    statements = (
        create_table_sql(table),
        create_pages_table_sql(pages_table, table),
    )

    for statement in statements:
        for identifier in BACKTICKED.findall(statement):
            assert len(identifier) <= 64, identifier

    # Two long names must still produce two distinct index names.
    assert create_table_sql("t" * 64) != create_table_sql("t" * 63 + "u")


def test_ddl_builders_refuse_unsafe_identifiers():
    for builder, args in (
        (create_database_sql, ("ocr; DROP TABLE users",)),
        (create_table_sql, ("ocr; DROP TABLE users",)),
        (create_pages_table_sql, ("bad name", "documents")),
    ):
        with pytest.raises(InvalidDatabaseSettingsError):
            builder(*args)


# ---------------------------------------------------------------------------
# connect / schema creation
# ---------------------------------------------------------------------------
def test_connect_creates_the_database_and_both_tables(fake_mysql):
    database, connection = connect_database(fake_mysql)

    assert database.server_version == "8.0.36-fake"
    assert database.database_created is True
    assert database.tables_created is True
    assert connection.current_database == "flask_ocr"
    assert connection.kwargs["autocommit"] is True
    assert connection.kwargs["charset"] == "utf8mb4"
    assert connection.kwargs["connect_timeout"] == 8

    creates = [s for s in connection.statements if s.lower().startswith("create")]
    assert len(creates) == 3
    assert creates[0].startswith("CREATE DATABASE IF NOT EXISTS")
    assert creates[1].startswith(f"CREATE TABLE IF NOT EXISTS `{DEFAULT_TABLE}`")
    assert creates[2].startswith(f"CREATE TABLE IF NOT EXISTS `{DEFAULT_TABLE}_pages`")


def test_connect_reports_an_existing_schema_and_drops_the_old_connection(fake_mysql):
    database, first = connect_database(fake_mysql)
    first.schemas.add("flask_ocr")
    first.tables.update({DEFAULT_TABLE, f"{DEFAULT_TABLE}_pages"})

    database.connect()

    assert database.database_created is False
    assert database.tables_created is False
    assert first.closed is True, "the previous connection must be closed"


def test_connect_failure_keeps_the_message_readable(monkeypatch):
    def _boom(**kwargs):
        raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")

    monkeypatch.setattr("app.database.pymysql.connect", _boom)
    database = MySqlDatabase(MySqlSettings(host="db.invalid"))

    with pytest.raises(DatabaseUnavailableError) as error:
        database.connect()

    assert "Could not connect to MySQL at root@db.invalid:3306/flask_ocr" in str(error.value)
    assert "Can't connect to MySQL server" in str(error.value)
    assert database.last_error is not None


def test_ensure_schema_recreates_a_dropped_table(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.tables.clear()

    assert database.ensure_schema() is True
    assert f"{DEFAULT_TABLE}_pages" in connection.tables


def test_the_created_table_carries_the_structured_field_columns(fake_mysql):
    """A fresh table is the whole contract the INSERT relies on."""
    _, connection = connect_database(fake_mysql)

    columns = connection.columns[DEFAULT_TABLE]
    assert set(FIELD_ORDER) <= columns
    assert columns == ddl_columns(create_table_sql(DEFAULT_TABLE)), (
        "the fake model and the real DDL agree"
    )



def test_connect_adds_the_structured_columns_to_an_older_table(fake_mysql):
    """A store created before the fields existed is upgraded, not abandoned."""
    database = MySqlDatabase(MySqlSettings(host="db.internal"))
    database.connect()  # creates the schema and both (current) tables
    first = fake_mysql[-1]
    # Now pretend the server holds the table as the *previous* version built it.
    first.columns[DEFAULT_TABLE] = ddl_columns(create_table_sql(DEFAULT_TABLE)) - set(
        FIELD_ORDER
    )

    database.connect()  # a new connection: same server, an older table

    connection = fake_mysql[-1]
    alters = [s for s in connection.statements if s.lower().startswith("alter table")]
    assert len(alters) == len(FIELD_ORDER)
    assert all("ADD COLUMN" in statement and "NULL" in statement for statement in alters)
    assert set(FIELD_ORDER) <= connection.columns[DEFAULT_TABLE]
    assert database.tables_created is False, "adding columns is not creating tables"


def test_a_second_connect_adds_nothing(fake_mysql):
    """The upgrade is idempotent - a restart must not repeat the DDL."""
    database, connection = connect_database(fake_mysql)

    database.connect()

    assert [
        s for s in fake_mysql[-1].statements if s.lower().startswith("alter table")
    ] == []




# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------
def test_save_extraction_stores_name_timestamp_and_content(fake_mysql):
    database, connection = connect_database(fake_mysql)
    result = sample_result()
    timestamp = datetime(2026, 7, 8, 9, 10, 11)

    record_id = database.save_extraction(result, uploaded_at=timestamp)

    assert record_id == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0

    insert, params = next(call for call in connection.calls if call[0].startswith("INSERT INTO"))
    assert columns_of(insert)[:3] == ["filename", "uploaded_at", "content"]
    assert params[0] == "invoice.pdf"
    assert params[1] == timestamp
    assert params[2] == result.full_text()
    assert params[3] == "pdf"
    assert params[4] == 2, "page count"
    assert params[-1] == content_sha256(result.full_text())


def test_save_extraction_writes_one_page_row_per_page(fake_mysql):
    database, connection = connect_database(fake_mysql)

    database.save_extraction(sample_result())

    assert len(connection.page_rows) == 2
    first_page = connection.page_rows[0]
    assert first_page["extraction_id"] == 1, "the page row points at the extraction"
    assert first_page["page_number"] == 1
    assert first_page["method"] == "ocr"
    assert first_page["content"] == "ACME invoice 2026"
    assert connection.page_rows[1]["page_number"] == 2


def test_save_extraction_defaults_the_timestamp_to_utc_now(fake_mysql):
    database, connection = connect_database(fake_mysql)

    database.save_extraction(sample_result())

    _, params = next(call for call in connection.calls if call[0].startswith("INSERT INTO"))
    stored_at = params[1]
    assert stored_at.tzinfo is None, "DATETIME columns hold naive UTC values"
    assert abs((datetime.now(timezone.utc) - stored_at.replace(tzinfo=timezone.utc)).total_seconds()) < 30


def test_save_extraction_rolls_back_and_reports_a_failed_insert(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.fail_on = "insert into"

    with pytest.raises(DatabaseWriteError, match="Saving the extraction failed"):
        database.save_extraction(sample_result())

    assert connection.rollbacks == 1
    assert connection.records == [], "nothing may be half written"


def test_save_extraction_requires_a_connection():
    with pytest.raises(DatabaseNotConfiguredError, match="No MySQL server is connected"):
        MySqlDatabase(MySqlSettings()).save_extraction(sample_result())


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def test_recent_extractions_are_json_friendly_and_limited(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1), stored_row(2, filename="second.pdf")]

    records = database.recent_extractions(limit=1)

    assert len(records) == 1
    assert records[0]["filename"] == "second.pdf", "newest first"
    assert records[0]["uploaded_at"] == "2026-01-02 03:04:05 UTC"
    assert records[0]["confidence"] == 93.25, "Decimal becomes a float"
    assert len(records[0]["preview"]) <= 140

    listing, params = connection.calls[-1]
    assert "LIMIT %s" in listing, "the limit is bound, never interpolated"
    assert params[1] == 1


def test_recent_extractions_clamps_a_silly_limit(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1)]

    database.recent_extractions(limit="0")
    assert connection.calls[-1][1][1] == 25, "falls back to the default"

    database.recent_extractions(limit="nonsense")
    assert connection.calls[-1][1][1] == 25


# ---------------------------------------------------------------------------
# paging (the records view reads one page at a time)
# ---------------------------------------------------------------------------
def test_paging_helpers_clamp_a_page_and_its_offset():
    assert clamp_record_page(None) == 1
    assert clamp_record_page("") == 1
    assert clamp_record_page(3) == 3
    assert clamp_record_page(" 3 ") == 3
    assert clamp_record_page("0") == 1, "pages are 1-based"
    assert clamp_record_page("-2") == 1
    assert clamp_record_page("nonsense") == 1
    assert clamp_record_page(10_000_000) == MAX_RECORD_PAGE, "an absurd page is capped"

    assert clamp_record_offset(None) == 0
    assert clamp_record_offset(20) == 20
    assert clamp_record_offset("-5") == 0, "a negative offset starts at the beginning"
    assert clamp_record_offset("nonsense") == 0
    assert clamp_record_offset(2**70) == (MAX_RECORD_PAGE - 1) * MAX_LIST_LIMIT


def test_search_extractions_pages_with_a_bound_offset(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="one.pdf"),
        stored_row(2, filename="two.pdf"),
        stored_row(3, filename="three.pdf"),
    ]

    first = database.search_extractions("", 2)
    assert [row["filename"] for row in first] == ["three.pdf", "two.pdf"], "newest first"
    statement, params = connection.calls[-1]
    assert "OFFSET" not in statement, "page one runs the query it always ran"
    assert params[-1] == 2

    second = database.search_extractions("", 2, offset=2)
    assert [row["filename"] for row in second] == ["one.pdf"], "the second page continues"
    statement, params = connection.calls[-1]
    assert "LIMIT %s OFFSET %s" in statement, "the offset is bound, never interpolated"
    assert params[-2:] == (2, 2), "limit first, offset last"

    assert [
        row["filename"] for row in database.search_extractions("", 2, offset="nonsense")
    ] == ["three.pdf", "two.pdf"], "a broken offset pages from the start"


def test_clamp_record_limit_falls_back_to_the_page_size_it_is_given():
    assert clamp_record_limit("nonsense") == DEFAULT_LIST_LIMIT
    assert clamp_record_limit("nonsense", 10) == 10, "the view passes its configured page size"
    assert clamp_record_limit("0", 10) == 10
    assert clamp_record_limit("-2", 10) == 10
    assert clamp_record_limit(" 4 ", 10) == 4
    assert clamp_record_limit("9999", 10) == MAX_LIST_LIMIT


def test_count_extractions_counts_the_matching_rows(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="invoice.pdf", content="ACME invoice 2026"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    assert database.count_extractions() == 2
    statement, params = connection.calls[-1]
    assert statement.startswith("SELECT COUNT(*) AS `total` FROM `ocr_extractions`")
    assert "WHERE" not in statement and params == (), "no term, no filtering"

    assert database.count_extractions("acme") == 1
    statement, params = connection.calls[-1]
    assert "`filename` LIKE %s ESCAPE '!'" in statement, "the same clause as the listing"
    assert params == ("%acme%", "%acme%", None), "one pattern per column, id last"

    assert database.count_extractions("letter", scope="filename") == 1
    assert database.count_extractions("letter", scope="content") == 0
    assert database.count_extractions("nothing like this") == 0


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------
def test_search_helpers_normalise_terms_patterns_and_scopes():
    assert clean_search_term("  two\n  lines ") == "two lines", "whitespace is collapsed"
    assert clean_search_term(None) == ""
    assert len(clean_search_term("x" * 500)) == MAX_SEARCH_CHARS

    assert like_pattern("invoice") == "%invoice%"
    assert like_pattern("a%b_c!d") == "%a!%b!_c!!d%", "wildcards and the escape are escaped"

    assert normalize_search_scope("CONTENT") == "content"
    assert normalize_search_scope("FILENAME") == "filename"
    assert normalize_search_scope("file name") == "all", "unknown scopes fall back"
    assert normalize_search_scope(None) == "all"


def test_search_extractions_binds_escaped_like_patterns(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1, filename="invoice.pdf", content="ACME invoice 2026")]

    records = database.search_extractions("invoice")

    assert [row["id"] for row in records] == [1]
    statement, params = connection.calls[-1]
    assert "`filename` LIKE %s ESCAPE '!'" in statement, "both columns are searched"
    assert "`content` LIKE %s ESCAPE '!'" in statement
    assert params[1] == params[2] == "%invoice%", "one bound pattern per column"
    assert params[3] is None, "a word is not a record id"
    assert params[-1] == 25, "the limit stays last and is bound"
    assert "LIMIT %s" in statement


def test_search_extractions_matches_the_file_name_and_the_stored_text(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="archived.pdf", content="ACME invoice 2026"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    assert [row["id"] for row in database.search_extractions("acme")] == [1], "case-insensitive"
    assert [row["id"] for row in database.search_extractions("letter")] == [2]
    assert database.search_extractions("nothing like this") == []


def test_search_extractions_treats_typed_wildcards_literally(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="report_100%.pdf"),
        stored_row(2, filename="report-1000.pdf"),
    ]

    assert [row["id"] for row in database.search_extractions("100%")] == [1]
    assert connection.calls[-1][1][1] == "%100!%%"

    assert [row["id"] for row in database.search_extractions("report_1")] == [1], (
        "the underscore is literal, not 'any character' (that would match report-1000.pdf)"
    )
    assert connection.calls[-1][1][1] == "%report!_1%"

    assert database.search_extractions("100!") == [], "an exclamation is not a wildcard"
    assert connection.calls[-1][1][1] == "%100!!%", "the escape character is doubled"


def test_search_extractions_scope_narrows_the_columns(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="invoice.pdf", content="nothing to see here"),
        stored_row(2, filename="letter.pdf", content="total 1234.56 USD"),
    ]

    assert [row["id"] for row in database.search_extractions("invoice", scope="filename")] == [1]
    statement, params = connection.calls[-1]
    assert "`content` LIKE" not in statement, "the text is not searched"
    assert params[1] == "%invoice%" and params[2] is None

    assert [row["id"] for row in database.search_extractions("1234.56", scope="content")] == [2]
    statement, params = connection.calls[-1]
    assert "`filename` LIKE" not in statement, "the file name is not searched"
    assert params[1] == "%1234.56%" and params[2] is None

    assert database.search_extractions("invoice", scope="content") == [], "scope is respected"
    assert [row["id"] for row in database.search_extractions("invoice")] == [1], "all is the default"


def test_search_extractions_falls_back_to_a_known_scope(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1, filename="invoice.pdf", content="invoice total")]

    assert len(database.search_extractions("invoice", scope="nonsense")) == 1

    statement = connection.calls[-1][0]
    assert "`filename` LIKE" in statement and "`content` LIKE" in statement


def test_search_extractions_matches_a_record_number(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(7, filename="elsewhere.pdf", content="nothing here")]

    assert [row["id"] for row in database.search_extractions("7")] == [7]
    assert connection.calls[-1][1][-2] == 7, "the id is bound, never interpolated"

    assert database.search_extractions("9" * 30) == [], "beyond BIGINT UNSIGNED"
    assert connection.calls[-1][1][-2] is None


def test_search_extractions_lists_everything_for_a_blank_term(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1), stored_row(2, filename="second.pdf")]

    records = database.search_extractions("   ")

    assert len(records) == 2
    statement, params = connection.calls[-1]
    assert "LIKE" not in statement.upper(), "no pointless pattern when nothing is searched"
    assert params[1] == 25


def test_search_extractions_truncates_a_very_long_term(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1)]

    database.search_extractions("x" * 500)

    assert len(connection.calls[-1][1][1]) == MAX_SEARCH_CHARS + 2, "the %...% wrapper remains"


def test_record_count_and_delete(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1), stored_row(2)]

    assert database.record_count() == 2
    assert database.delete_extraction(2) is True
    assert database.delete_extraction(99) is False
    assert [row["id"] for row in connection.records] == [1]


def test_get_extraction_returns_the_pages(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(7)]

    record = database.get_extraction(7)

    assert record is not None
    assert record["id"] == 7
    assert record["uploaded_at"] == "2026-01-02 03:04:05 UTC"
    assert record["pages"] == []

    assert database.get_extraction(8) is None


def test_require_extraction_raises_a_not_found_error(fake_mysql):
    database, _ = connect_database(fake_mysql)

    with pytest.raises(DatabaseRecordNotFoundError, match="Record #5"):
        database.require_extraction(5)


def test_status_reports_the_schema_decisions(fake_mysql):
    database, _ = connect_database(fake_mysql)

    status = database.status()

    assert status["connected"] is True
    assert status["provider"] == "mysql"
    assert status["database_created"] is True
    assert status["settings"]["database"] == "flask_ocr"
    assert "password" not in status["settings"]
    assert status["connected_at"].endswith("UTC")


# ---------------------------------------------------------------------------
# connection UI
# ---------------------------------------------------------------------------
def test_database_page_renders_the_connection_form(client):
    response = client.get("/database")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "MySQL storage" in body
    assert 'id="db-host"' in body
    assert "Connect &amp; create schema" in body
    assert "Not connected" in body
    assert "ocr_extractions" in body


def test_database_page_is_linked_from_every_page(client):
    assert "Database" in client.get("/").get_data(as_text=True)


def test_database_page_reports_a_failed_connect_and_keeps_the_form(
    make_client, tmp_path, monkeypatch
):
    def _boom(**kwargs):
        raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")

    monkeypatch.setattr("app.database.pymysql.connect", _boom)
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))

    response = client.post(
        "/database/connect",
        data={"host": "db.invalid", "port": "3307", "user": "ocr", "password": "s3cret"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "Could not connect to MySQL" in body
    assert 'value="db.invalid"' in body, "what the user typed is kept"
    assert 'value="3307"' in body
    assert "s3cret" not in body, "the password is never echoed back"


def test_database_page_rejects_an_incomplete_form(make_client, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))

    response = client.post("/database/connect", data={"host": "", "user": "root"})

    assert response.status_code == 400
    assert "Enter the host name of the MySQL server" in response.get_data(as_text=True)


def test_database_page_connect_creates_the_schema(make_client, fake_mysql, tmp_path):
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))

    response = client.post(
        "/database/connect",
        data={
            "host": "db.internal",
            "port": "3307",
            "user": "ocr",
            "password": "s3cret",
            "database": "ocr_store",
            "remember": "1",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Created the schema &#39;ocr_store&#39;" in body or "Created the schema 'ocr_store'" in body
    assert "Connected to ocr@db.internal:3307/ocr_store" in body
    assert settings_file.is_file(), "the credentials are remembered"

    connection = fake_mysql[0]
    assert "CREATE DATABASE IF NOT EXISTS `ocr_store` CHARACTER SET utf8mb4" in connection.statements
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS `ocr_extractions`") for s in connection.statements)
    assert connection.kwargs["user"] == "ocr" and connection.kwargs["port"] == 3307


def test_connect_form_remembers_the_details_when_the_box_is_ticked(
    make_client, fake_mysql, tmp_path
):
    """The form posts the hidden ``0`` **and** the ticked box ``1`` - the box wins."""
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))

    response = client.post(
        "/database/connect",
        data={
            "host": "db.internal",
            "port": "3306",
            "user": "ocr",
            "database": "flask_ocr",
            "remember": ["0", "1"],
        },
    )

    assert response.status_code == 200
    assert settings_file.is_file(), "the ticked box remembers the details"


def test_connect_form_does_not_remember_an_unticked_box(
    make_client, fake_mysql, tmp_path
):
    """An unticked box submits only the hidden ``0``, so nothing is written."""
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))

    response = client.post(
        "/database/connect",
        data={
            "host": "db.internal",
            "user": "ocr",
            "database": "flask_ocr",
            "remember": "0",
        },
    )

    assert response.status_code == 200
    assert not settings_file.exists()


def test_api_connect_remembers_only_when_the_payload_asks_for_it(
    make_client, fake_mysql, tmp_path
):
    """The JSON API carries a real boolean, and it wins over the configured default."""
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))
    body = {"host": "db.internal", "user": "ocr", "database": "flask_ocr"}

    asked = client.post("/api/database/connect", json={**body, "remember": True})
    assert asked.status_code == 200
    assert settings_file.is_file()

    settings_file.unlink()
    refused = client.post("/api/database/connect", json={**body, "remember": False})
    assert refused.status_code == 200
    assert not settings_file.exists(), "remember=false must not write the file"


def test_api_connect_reads_the_hidden_checkbox_pair_too(make_client, fake_mysql, tmp_path):
    """A form post to the same endpoint sends the hidden ``0`` *and* the ticked ``1``."""
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))

    response = client.post(
        "/api/database/connect",
        data={
            "remember": ["0", "1"],  # what the form sends: hidden 0, then the box
            "host": "db.internal",
            "user": "ocr",
            "database": "flask_ocr",
        },
    )

    assert response.status_code == 200
    assert settings_file.is_file(), "the ticked box wins over the hidden 0"


def test_database_page_disconnect_and_forget(make_client, fake_mysql, tmp_path):
    settings_file = tmp_path / "mysql.json"
    client = make_client(MYSQL_SETTINGS_FILE=str(settings_file))
    client.post("/database/connect", data={"host": "db.internal", "user": "ocr", "remember": "1"})
    assert settings_file.is_file()

    disconnected = client.post("/database/disconnect")
    assert disconnected.status_code == 303
    assert "Not connected" in client.get("/database").get_data(as_text=True)

    forgotten = client.post("/database/forget")
    assert forgotten.status_code == 200
    assert "were deleted" in forgotten.get_data(as_text=True)
    assert not settings_file.exists()


# ---------------------------------------------------------------------------
# manager: remembering the connection
# ---------------------------------------------------------------------------
def test_manager_remembers_and_forgets_the_connection(fake_mysql, tmp_path):
    settings_file = tmp_path / "mysql.json"
    manager = DatabaseManager(settings_file=settings_file)

    manager.connect(
        {"host": "db.internal", "user": "ocr", "password": "s3cret", "database": "ocr_store"},
        remember=True,
    )

    assert settings_file.is_file()
    payload = json.loads(settings_file.read_text(encoding="utf-8"))
    assert payload["host"] == "db.internal"
    assert payload["password"] == "s3cret"
    assert manager.saved_settings().database == "ocr_store"
    assert manager.suggest_settings().host == "db.internal"
    assert manager.status()["saved_settings"] is True

    assert manager.forget_settings() is True
    assert manager.saved_settings() is None
    assert manager.suggest_settings().host == manager.defaults.host
    assert manager.forget_settings() is False, "already gone"


def test_manager_can_connect_without_remembering(fake_mysql, tmp_path):
    settings_file = tmp_path / "mysql.json"
    manager = DatabaseManager(settings_file=settings_file)

    manager.connect({"host": "db.internal", "user": "ocr"}, remember=False)

    assert not settings_file.exists()
    assert manager.is_connected is True


def test_manager_keeps_a_working_connection_when_a_new_one_fails(
    fake_mysql, tmp_path, monkeypatch
):
    manager = DatabaseManager(settings_file=tmp_path / "mysql.json")
    manager.connect({"host": "first.internal", "user": "ocr"}, remember=False)
    working = manager.database

    def _boom(**kwargs):
        raise pymysql.err.OperationalError(2003, "host unreachable")

    monkeypatch.setattr("app.database.pymysql.connect", _boom)

    with pytest.raises(DatabaseUnavailableError):
        manager.connect({"host": "second.internal", "user": "ocr"}, remember=False)

    assert manager.database is working
    assert manager.is_connected is True
    assert working._connection.closed is False


def test_manager_disconnect_closes_the_connection(fake_mysql, tmp_path):
    manager = DatabaseManager(settings_file=tmp_path / "mysql.json")
    manager.connect({"host": "db.internal", "user": "ocr"}, remember=False)
    connection = manager.database._connection

    manager.disconnect()

    assert connection.closed is True
    assert manager.is_connected is False
    assert manager.status()["connected"] is False


def test_manager_reports_a_missing_driver(monkeypatch, tmp_path):
    monkeypatch.setattr("app.database.pymysql", None)
    manager = DatabaseManager(settings_file=tmp_path / "mysql.json")

    status = manager.status()
    assert status["driver"]["available"] is False
    assert status["driver"]["version"] is None
    assert status["install_hint"]

    with pytest.raises(DatabaseUnavailableError, match="not installed"):
        manager.connect({"host": "localhost", "user": "root"}, remember=False)


def test_manager_ignores_a_corrupt_settings_file(tmp_path):
    settings_file = tmp_path / "mysql.json"
    settings_file.write_text("{not json", encoding="utf-8")

    manager = DatabaseManager(settings_file=settings_file)

    assert manager.saved_settings() is None
    assert manager.status()["saved_settings"] is False


def test_form_defaults_come_from_the_environment_and_hide_the_password(tmp_path):
    manager = DatabaseManager(
        settings_file=tmp_path / "mysql.json",
        defaults=MySqlSettings(host="env.host", user="envuser", password="envpass"),
    )

    form = manager.form_defaults()

    assert form["host"] == "env.host"
    assert form["user"] == "envuser"
    assert form["saved"] is False
    assert "password" not in form
    assert form["has_password"] is True


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
def test_api_database_status_without_a_connection(client):
    payload = client.get("/api/database").get_json()["database"]

    assert payload["provider"] == "mysql"
    assert payload["connected"] is False
    assert payload["driver"]["available"] is True
    assert payload["record_count"] is None
    assert payload["settings"]["table"] == DEFAULT_TABLE
    assert "password" not in payload["settings"]


def test_api_connect_creates_the_schema_and_returns_the_status(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))

    response = client.post(
        "/api/database/connect",
        json={
            "host": "db.internal",
            "port": 3307,
            "user": "ocr",
            "password": "s3cret",
            "database": "ocr_store",
            "remember": False,
        },
    )
    payload = response.get_json()["database"]

    assert response.status_code == 200
    assert payload["connected"] is True
    assert payload["database_created"] is True
    assert payload["tables_created"] is True
    assert payload["server_version"] == "8.0.36-fake"
    assert payload["record_count"] == 0
    assert "Created the schema 'ocr_store'" in payload["message"]
    assert not (tmp_path / "mysql.json").exists(), "remember=false must not write a file"


def test_api_connect_reports_invalid_settings_as_json(make_client, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))

    response = client.post(
        "/api/database/connect",
        json={"host": "localhost", "user": "root", "database": "bad name"},
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_database_settings"
    assert "valid MySQL name" in error["message"]


def test_api_records_require_a_connection(client):
    response = client.get("/api/database/records")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "database_not_configured"


def test_api_records_list_detail_and_delete(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    connection = fake_mysql[0]
    connection.records = [stored_row(1), stored_row(2, filename="second.pdf")]

    listing = client.get("/api/database/records?limit=1").get_json()
    assert listing["ok"] is True
    assert len(listing["records"]) == 1
    assert listing["database"]["record_count"] == 2
    assert listing["records"][0]["uploaded_at"].endswith("UTC")

    detail = client.get("/api/database/records/1").get_json()["record"]
    assert detail["id"] == 1
    assert detail["filename"] == "archived.pdf"

    missing = client.get("/api/database/records/42")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "database_record_not_found"

    deleted = client.delete("/api/database/records/2")
    assert deleted.status_code == 200
    assert deleted.get_json()["deleted"] == 2

    assert client.delete("/api/database/records/2").status_code == 404


def test_api_records_search_reports_the_applied_filters(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [
        stored_row(1, filename="invoice.pdf", content="ACME invoice 2026"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    hits = client.get("/api/database/records?q=ACME&limit=5").get_json()

    assert hits["ok"] is True
    assert hits["query"] == {
        "q": "ACME",
        "scope": "all",
        "limit": 5,
        "page": 1,
        "count": 1,
        "total": 1,
        "pages": 1,
    }
    assert [row["id"] for row in hits["records"]] == [1]
    assert hits["records"][0]["preview"].startswith("ACME invoice")

    none = client.get("/api/database/records?q=acme&scope=filename").get_json()
    assert none["query"]["count"] == 0, "the file name does not contain ACME"
    assert none["records"] == []

    seeded = client.get("/api/database/records?q=2&scope=content").get_json()
    assert seeded["query"]["limit"] == MAX_LIST_LIMIT, (
        "no ?limit= means every stored record, up to the storage layer's cap"
    )
    assert seeded["query"]["page"] == 1 and seeded["query"]["pages"] == 1
    assert [row["id"] for row in seeded["records"]] == [2, 1], (
        "a number finds the record id as well as the text containing it"
    )

    explicit = client.get("/api/database/records?limit=all").get_json()
    assert explicit["query"]["limit"] == MAX_LIST_LIMIT, "?limit=all asks for the store too"
    assert [row["id"] for row in explicit["records"]] == [2, 1]


def test_api_disconnect_closes_the_connection(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})

    payload = client.post("/api/database/disconnect").get_json()

    assert payload["was_connected"] is True
    assert payload["database"]["connected"] is False
    assert fake_mysql[0].closed is True


# ---------------------------------------------------------------------------
# stored record pages
# ---------------------------------------------------------------------------
def test_record_page_shows_the_stored_content(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1, content="stored page text")]

    response = client.get("/database/records/1")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "stored page text" in body
    assert "archived.pdf" in body
    assert "2026-01-02 03:04:05 UTC" in body


def test_record_download_serves_the_stored_text(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1, content="downloadable text")]

    response = client.get("/database/records/1/download")

    assert response.status_code == 200
    assert "downloadable text" in response.get_data(as_text=True)
    assert "archived.db.txt" in response.headers["Content-Disposition"]


def test_record_page_404s_for_an_unknown_id(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})

    response = client.get("/database/records/999")

    assert response.status_code == 404
    assert "not stored in" in response.get_data(as_text=True)


def test_record_delete_from_the_ui_redirects(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(5)]

    response = client.post("/database/records/5/delete")

    assert response.status_code == 303
    assert fake_mysql[0].records == []


def test_database_page_leaves_the_records_to_the_records_view(
    make_client, fake_mysql, tmp_path
):
    """The connection page shows the store; the rows are listed in the records view."""
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1), stored_row(2, filename="second.pdf")]

    body = client.get("/database").get_data(as_text=True)

    assert "Stored extractions" not in body, "the table lives in the records view only"
    assert "archived.pdf" not in body and "second.pdf" not in body
    assert "Search stored records" not in body, "no search toolbar either"
    assert ">Records view</a>" in body, "but the page links to the table"
    assert "ocr@db.internal:3306/flask_ocr" in body, "the status panel names the server"
    assert "<dt>Stored records</dt>" in body, "and still counts the stored rows"


def test_record_page_renders_the_stored_pages(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1)]
    fake_mysql[0].page_rows = [
        {
            "extraction_id": 1,
            "page_number": 1,
            "method": "ocr",
            "content": "page one text",
            "char_count": 13,
            "word_count": 3,
            "confidence": Decimal("91.50"),
            "duration_ms": 40,
        },
        {
            "extraction_id": 1,
            "page_number": 2,
            "method": "embedded",
            "content": "",
            "char_count": 0,
            "word_count": 0,
            "confidence": None,
            "duration_ms": 0,
        },
    ]

    body = client.get("/database/records/1").get_data(as_text=True)

    assert "Stored pages" in body
    assert "page one text" in body
    assert "Page 1 of 2" in body
    assert "91.5% conf." in body
    assert "Text layer" in body, "an embedded page is labelled as such"
    assert "No text was stored for this page." in body


# ---------------------------------------------------------------------------
# records view (browse + search)
# ---------------------------------------------------------------------------
def test_records_page_lists_and_searches_the_stored_records(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [
        stored_row(1, filename="invoice.pdf", content="ACME invoice 2026"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    body = client.get("/database/records").get_data(as_text=True)

    assert "Stored records" in body
    assert "invoice.pdf" in body and "letter.pdf" in body
    assert 'action="/database/records"' in body, "the search form targets this page"
    assert 'name="q"' in body and 'name="scope"' in body
    assert "All 2 stored records." in body, "a store that is not empty lists every record"
    assert "Connect to a MySQL server" not in body

    hits = client.get("/database/records?q=invoice&scope=filename").get_data(as_text=True)

    assert "invoice.pdf" in hits
    assert "letter.pdf" not in hits
    assert 'value="invoice"' in hits, "the term is echoed back into the box"
    assert "1 match" in hits
    assert "in file name only" in hits
    assert "Reset" in hits


def test_records_page_searches_the_stored_text_and_the_record_number(
    make_client, fake_mysql, tmp_path
):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [
        stored_row(1, filename="invoice.pdf", content="ACME invoice total"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    by_text = client.get("/database/records?q=ACME").get_data(as_text=True)
    assert "invoice.pdf" in by_text and "letter.pdf" not in by_text

    by_number = client.get("/database/records?q=2").get_data(as_text=True)
    assert "letter.pdf" in by_number and "invoice.pdf" not in by_number

    empty = client.get("/database/records?q=nothing-matches-this").get_data(as_text=True)
    assert "0 matches" in empty
    assert "No stored record matches that search" in empty
    assert 'value="nothing-matches-this"' in empty


def test_records_page_without_a_connection_offers_the_form(client):
    body = client.get("/database/records").get_data(as_text=True)

    assert "Not connected" in body
    assert "Nothing is connected yet, so no extraction is stored" in body
    assert '<a href="/database">connect to a MySQL server</a>' in body, (
        "the records page has no form of its own, so it links to the one that has"
    )
    assert '<a href="/database">MySQL storage</a>' in body, "and to the storage page"
    assert "Nothing is connected yet, so extractions are not being stored" in body, (
        "the empty table explains how to connect - there is no form above the records view"
    )


def test_records_page_reports_a_server_that_stopped_answering(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1)]
    fake_mysql[0].fail_on = "select"

    body = client.get("/database/records").get_data(as_text=True)

    assert "Counting the matching extractions failed" in body, (
        "the page count is the first query the view runs"
    )


def test_records_page_escapes_the_search_term(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1)]

    body = client.get("/database/records?q=<script>alert(1)</script>").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body, "the term is never rendered as markup"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body, "it is echoed back escaped"


def test_records_page_ignores_a_silly_limit(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1)]

    body = client.get("/database/records?limit=nonsense").get_data(as_text=True)

    assert 'value="all" selected' in body, (
        "a broken limit falls back to the configured default, i.e. the whole store"
    )
    assert "All 1 stored record." in body


def test_records_page_shows_every_stored_record_by_default(make_client, fake_mysql, tmp_path):
    """Not empty means *all* of it: 23 rows, one page, no pagination to click."""
    client = paged_client(make_client, fake_mysql, tmp_path, 23)

    body = page_text(client.get("/database/records"))

    assert "doc-23.pdf" in body and "doc-01.pdf" in body, "the whole store, not ten rows"
    assert "All 23 stored records." in body
    assert "records-pagination" not in body, "everything fits - no page buttons"


def test_records_page_offers_all_and_the_usual_page_sizes(make_client, fake_mysql, tmp_path):
    client = paged_client(make_client, fake_mysql, tmp_path, 23)

    default = page_text(client.get("/database/records"))

    assert 'name="limit"' in default, "the toolbar still lets you narrow the page"
    assert '<option value="all" selected>' in default
    assert "All (up to 200)" in default, "the cap is spelled out"
    for size in ("10", "25", "50", "100"):
        assert f'<option value="{size}">' in default

    narrowed = page_text(client.get("/database/records?limit=10"))
    assert '<option value="10" selected>' in narrowed
    assert "Showing rows 1-10 of 23 stored records (page 1 of 3)." in narrowed
    assert 'href="/database/records?scope=all&amp;limit=10&amp;page=2"' in narrowed, (
        "a narrowed page still pages"
    )

    explicit = page_text(client.get("/database/records?limit=all"))
    assert "All 23 stored records." in explicit, "?limit=all spells out what the default does"


def test_records_page_pages_at_the_cap_when_the_store_is_bigger(
    make_client, fake_mysql, tmp_path
):
    """More rows than one page can hold: the cap applies and the buttons come back."""
    client = paged_client(make_client, fake_mysql, tmp_path, MAX_LIST_LIMIT + 5)

    first = page_text(client.get("/database/records"))

    assert f"doc-{MAX_LIST_LIMIT + 5:02d}.pdf" in first, "the newest row is first"
    assert "Showing rows 1-200 of 205 stored records (page 1 of 2)." in first
    assert 'href="/database/records?scope=all&amp;limit=200&amp;page=2"' not in first, (
        "the cap is the default, so the link carries 'all' instead of a number"
    )
    assert "limit=all&amp;page=2" in first, "Next walks to the rest of the store"

    last = page_text(client.get("/database/records?limit=all&page=2"))
    assert "Showing rows 201-205 of 205 stored records (page 2 of 2)." in last


def test_a_lower_configured_page_size_still_pages(make_client, fake_mysql, tmp_path):
    """``MYSQL_RECORDS_LIMIT=10`` restores the old default; "All" is one choice away."""
    client = make_client(
        MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"), MYSQL_RECORDS_LIMIT=10
    )
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [
        stored_row(number, filename=f"doc-{number:02d}.pdf") for number in range(1, 13)
    ]

    narrow = page_text(client.get("/database/records"))
    assert "Showing rows 1-10 of 12 stored records (page 1 of 2)." in narrow
    assert '<option value="10" selected>' in narrow

    whole = page_text(client.get("/database/records?limit=all"))
    assert "All 12 stored records." in whole, "the toolbar's All ignores the default"
    assert "records-pagination" not in whole


def page_text(response) -> str:
    """A rendered page with runs of whitespace collapsed, so a sentence can be asserted."""
    return " ".join(response.get_data(as_text=True).split())


def paged_client(make_client, fake_mysql, tmp_path, count):
    """A connected client whose fake server holds *count* rows (``doc-<nn>.pdf``)."""
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [
        stored_row(number, filename=f"doc-{number:02d}.pdf", content=f"body {number}")
        for number in range(1, count + 1)
    ]
    return client


def test_records_page_shows_ten_rows_a_page_with_numbered_pages(
    make_client, fake_mysql, tmp_path
):
    """An explicit page size still pages - the default just happens to be "all"."""
    client = paged_client(make_client, fake_mysql, tmp_path, 25)

    first = page_text(client.get("/database/records?limit=10"))
    assert "doc-25.pdf" in first and "doc-16.pdf" in first, "ten rows, newest first"
    assert "doc-15.pdf" not in first, "the eleventh row belongs to page 2"
    assert "Showing rows 1-10 of 25 stored records (page 1 of 3)." in first
    assert 'href="/database/records?scope=all&amp;limit=10&amp;page=2"' in first, "Next links on"
    assert "&laquo; Previous" in first and "is-disabled" in first, "nothing before page 1"
    assert '<span class="button button-tiny is-current" aria-current="page">1</span>' in first

    second = page_text(client.get("/database/records?limit=10&page=2"))
    assert "doc-15.pdf" in second and "doc-06.pdf" in second
    assert "doc-16.pdf" not in second and "doc-05.pdf" not in second
    assert "Showing rows 11-20 of 25 stored records (page 2 of 3)." in second
    assert 'href="/database/records?scope=all&amp;limit=10"' in second, "Previous goes back"
    assert 'href="/database/records?scope=all&amp;limit=10&amp;page=3"' in second
    assert "Page 2 of 3" in second

    last = page_text(client.get("/database/records?limit=10&page=3"))
    assert "doc-05.pdf" in last and "doc-01.pdf" in last, "the remainder"
    assert "doc-06.pdf" not in last
    assert "Showing rows 21-25 of 25 stored records (page 3 of 3)." in last
    assert 'aria-disabled="true">Next &raquo;' in last, "Next is off on the last page"


def test_records_page_clamps_a_page_past_the_end_and_a_silly_one(
    make_client, fake_mysql, tmp_path
):
    client = paged_client(make_client, fake_mysql, tmp_path, 25)

    beyond = page_text(client.get("/database/records?limit=10&page=99"))
    assert "doc-01.pdf" in beyond, "a page past the end lands on the last page"
    assert "page 3 of 3" in beyond

    for silly in ("0", "-4", "nonsense", ""):
        body = page_text(client.get(f"/database/records?limit=10&page={silly}"))
        assert "doc-25.pdf" in body and "page 1 of 3" in body, f"?page={silly!r} is page 1"


def test_records_page_pages_a_search_and_keeps_the_filters(make_client, fake_mysql, tmp_path):
    client = paged_client(make_client, fake_mysql, tmp_path, 25)

    body = page_text(client.get("/database/records?q=doc-1&limit=5"))

    assert "doc-19.pdf" in body and "doc-15.pdf" in body, "the first five matches"
    assert "doc-14.pdf" not in body
    assert "10 matches for &ldquo;doc-1&rdquo; in file name and text, rows 1-5" in body, (
        "the summary counts every match, not the page"
    )
    assert "(page 1 of 2)" in body
    assert "/database/records?scope=all&amp;limit=5&amp;q=doc-1&amp;page=2" in body, (
        "the search survives the page buttons"
    )


def test_records_view_is_linked_from_the_upload_and_storage_pages(
    make_client, fake_mysql, tmp_path
):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))

    assert 'href="/database/records"' in client.get("/").get_data(as_text=True)
    assert "/database/records" in client.get("/database").get_data(as_text=True)

    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(1)]
    assert 'href="/database/records"' in client.get("/database/records/1").get_data(as_text=True)


def test_delete_from_the_records_view_returns_to_the_search(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(5)]

    body = client.get("/database/records?q=archived").get_data(as_text=True)
    assert 'name="next" value="/database/records?q=archived"' in body

    response = client.post(
        "/database/records/5/delete", data={"next": "/database/records?q=archived"}
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/database/records?q=archived"
    assert fake_mysql[0].records == []


def test_delete_ignores_an_off_site_next_target(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = [stored_row(6)]

    response = client.post(
        "/database/records/6/delete", data={"next": "https://evil.example.com/steal"}
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/database/records", (
        "only same-site paths are honoured, and the records view is the fallback"
    )


# ---------------------------------------------------------------------------
# Excel export (.xlsx)
# ---------------------------------------------------------------------------
def workbook_text(payload: bytes, part: str | None = None) -> str:
    """One XML part (or all of them) of a generated workbook, as text to assert on."""
    assert zipfile.is_zipfile(io.BytesIO(payload)), "the download is not an OPC package"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [part] if part else archive.namelist()
        return "\n".join(archive.read(name).decode("utf-8") for name in names)


def test_export_extractions_selects_the_whole_text_instead_of_a_preview(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="invoice.pdf", content="x" * 400),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    records = database.export_extractions(limit=10)

    assert [row["id"] for row in records] == [2, 1], "same order as the records view"
    assert records[1]["content"] == "x" * 400, "the whole document is exported"
    assert records[1]["content_sha256"] == "0" * 64
    statement, params = connection.calls[-1]
    assert "LEFT(" not in statement
    assert params == (10,), "no preview length is bound"


def test_export_extractions_applies_the_records_search(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [
        stored_row(1, filename="invoice.pdf", content="ACME invoice total"),
        stored_row(2, filename="letter.pdf", content="Dear Sir"),
    ]

    records = database.export_extractions("acme", scope="content")

    assert [row["id"] for row in records] == [1]
    statement, params = connection.calls[-1]
    assert "`content` LIKE %s ESCAPE '!'" in statement
    assert params == ("%acme%", None, 25), "escaped pattern, id shortcut, limit"


def test_export_extractions_clamps_a_silly_limit(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.records = [stored_row(1)]

    database.export_extractions(limit="9999")

    assert connection.calls[-1][1][-1] == MAX_LIST_LIMIT


def test_export_extractions_requires_a_connection():
    database = MySqlDatabase(MySqlSettings(host="db.internal", user="ocr"))

    with pytest.raises(DatabaseNotConfiguredError):
        database.export_extractions()


def test_pages_for_extractions_reads_every_page_in_one_query(fake_mysql):
    database, connection = connect_database(fake_mysql)
    connection.page_rows = [
        stored_page(1, 2, content="second page"),
        stored_page(1, 1, content="first page"),
        stored_page(2, 1, content="another record"),
        stored_page(3, 1, content="not requested"),
    ]

    pages = database.pages_for_extractions([1, 2])

    assert [(page["extraction_id"], page["page_number"]) for page in pages] == [
        (1, 1),
        (1, 2),
        (2, 1),
    ], "ordered by record, then by page"
    assert pages[0]["content"] == "first page"
    assert pages[0]["confidence"] == 96.5, "Decimal becomes a float"
    statement, params = connection.calls[-1]
    assert "`extraction_id` IN (%s, %s)" in statement
    assert params == (1, 2), "the ids are bound, never interpolated"


def test_pages_for_extractions_skips_the_query_without_ids(fake_mysql):
    database, connection = connect_database(fake_mysql)
    before = len(connection.calls)

    assert database.pages_for_extractions([]) == []

    assert len(connection.calls) == before, "nothing to read - no query is sent"



def export_client(make_client, fake_mysql, tmp_path, records, pages=()):
    """A connected client whose fake server already holds *records*/*pages*."""
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    fake_mysql[0].records = list(records)
    fake_mysql[0].page_rows = list(pages)
    return client


def test_records_export_downloads_the_rows_shown_as_a_workbook(
    make_client, fake_mysql, tmp_path
):
    client = export_client(
        make_client,
        fake_mysql,
        tmp_path,
        [
            stored_row(1, filename="invoice.pdf", content="ACME invoice total"),
            stored_row(2, filename="letter.pdf", content="Dear Sir"),
        ],
        [
            stored_page(1, 1, content="invoice first page"),
            stored_page(2, 1, content="letter page"),
        ],
    )

    response = client.get("/database/records/export.xlsx?q=ACME&limit=5")

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == XLSX_MIMETYPE
    disposition = response.headers["Content-Disposition"]
    assert "attachment" in disposition and ".xlsx" in disposition
    assert "records_ACME" in disposition, "the file name names the search"

    parts = workbook_text(response.get_data())
    assert "invoice.pdf" in parts and "ACME invoice total" in parts, "the full text"
    assert "letter.pdf" not in parts, "only the matching rows are exported"
    assert "invoice first page" in parts, "the pages of the exported records"
    assert "letter page" not in parts
    assert "Search term" in parts and "Row limit" in parts, "the Export sheet explains itself"

    pages_sheet = workbook_text(response.get_data(), "xl/worksheets/sheet2.xml")
    assert "invoice first page" in pages_sheet
    assert "invoice.pdf" in pages_sheet, "each page row carries its file name"


def test_records_export_honours_the_search_scope(make_client, fake_mysql, tmp_path):
    client = export_client(
        make_client,
        fake_mysql,
        tmp_path,
        [stored_row(1, filename="invoice.pdf", content="ACME invoice total")],
    )

    by_text = workbook_text(client.get("/database/records/export.xlsx?q=ACME").get_data())
    assert "invoice.pdf" in by_text

    by_name = workbook_text(
        client.get("/database/records/export.xlsx?q=ACME&scope=filename").get_data()
    )
    assert "invoice.pdf" not in by_name, "the file name does not contain ACME"
    assert "Records" in by_name, "an empty export is still a valid workbook"


def test_records_export_follows_the_limit_of_the_view(make_client, fake_mysql, tmp_path):
    client = export_client(
        make_client,
        fake_mysql,
        tmp_path,
        [
            stored_row(1, filename="archived.pdf"),
            stored_row(2, filename="second.pdf"),
        ],
    )

    everything = workbook_text(client.get("/database/records/export.xlsx").get_data())
    assert "archived.pdf" in everything and "second.pdf" in everything

    newest = workbook_text(client.get("/database/records/export.xlsx?limit=1").get_data())
    assert "second.pdf" in newest, "newest first"
    assert "archived.pdf" not in newest, "the row limit is honoured"


def test_records_export_follows_the_page_shown(make_client, fake_mysql, tmp_path):
    client = paged_client(make_client, fake_mysql, tmp_path, 25)

    body = page_text(client.get("/database/records?limit=10&page=3"))
    assert "?scope=all&amp;limit=10&amp;page=3" in body, "the button exports this page"

    workbook = workbook_text(
        client.get("/database/records/export.xlsx?limit=10&page=3").get_data()
    )
    assert "doc-01.pdf" in workbook and "doc-05.pdf" in workbook
    assert "doc-25.pdf" not in workbook, "the export holds the page you are looking at"
    assert "Row limit" in workbook and "Page" in workbook, "the Export sheet explains itself"


def test_records_export_takes_the_whole_store_by_default(make_client, fake_mysql, tmp_path):
    client = paged_client(make_client, fake_mysql, tmp_path, 23)

    body = client.get("/database/records").get_data(as_text=True)
    assert "export.xlsx?scope=all&amp;limit=all" in body, (
        "the export button carries the choice the table is showing"
    )

    workbook = workbook_text(client.get("/database/records/export.xlsx?limit=all").get_data())
    assert "doc-23.pdf" in workbook and "doc-01.pdf" in workbook, "every stored record"
    assert "all stored records" in workbook, "the Export sheet says so"


def test_records_export_without_a_connection_explains_itself(client):
    response = client.get("/database/records/export.xlsx")

    assert response.status_code == 400
    assert "No MySQL server is connected yet" in response.get_data(as_text=True)


def test_records_export_reports_a_server_that_stopped_answering(
    make_client, fake_mysql, tmp_path
):
    client = export_client(make_client, fake_mysql, tmp_path, [stored_row(1)])
    fake_mysql[0].fail_on = "select"

    response = client.get("/database/records/export.xlsx")

    assert response.status_code == 500
    assert "Counting the matching extractions failed" in response.get_data(as_text=True)


def test_record_export_downloads_one_record_with_its_pages(make_client, fake_mysql, tmp_path):
    client = export_client(
        make_client,
        fake_mysql,
        tmp_path,
        [stored_row(1, filename="invoice.pdf", content="stored page text")],
        [
            stored_page(1, 1, content="first page"),
            stored_page(1, 2, content="second page"),
        ],
    )

    response = client.get("/database/records/1/export.xlsx")

    assert response.status_code == 200, response.get_data(as_text=True)
    assert "invoice_record_1" in response.headers["Content-Disposition"]
    parts = workbook_text(response.get_data())
    assert "stored page text" in parts, "the stored content is in the workbook"
    assert "first page" in parts and "second page" in parts, "one row per page"
    assert "invoice.pdf" in parts
    assert "#1 - invoice.pdf" in parts, "the Export sheet names the record"


def test_record_export_404s_for_an_unknown_id(make_client, fake_mysql, tmp_path):
    client = export_client(make_client, fake_mysql, tmp_path, [stored_row(1)])

    response = client.get("/database/records/42/export.xlsx")

    assert response.status_code == 404
    assert "not stored in" in response.get_data(as_text=True)


def test_the_records_view_offers_the_export(make_client, fake_mysql, tmp_path):
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})

    assert "/database/records/export.xlsx" not in client.get("/database/records").get_data(
        as_text=True
    ), "nothing stored - nothing to export"

    fake_mysql[0].records = [stored_row(1, filename="invoice.pdf")]
    body = client.get("/database/records?q=invoice&limit=5").get_data(as_text=True)

    assert "Export .xlsx" in body
    assert (
        "/database/records/export.xlsx?scope=all&amp;limit=5&amp;q=invoice" in body
    ), "the export carries the filters of the table above it"



# ---------------------------------------------------------------------------
# uploads -> MySQL
# ---------------------------------------------------------------------------
DIGITAL_PDF_TEXT = (
    "This digital page already carries a selectable text layer for the database test."
)


def upload_document(client, document: bytes, filename: str, *, save_to_db: str | None = None):
    """Post a file exactly like the browser form does (optionally opting out)."""
    data = {"file": (io.BytesIO(document), filename)}
    if save_to_db is not None:
        data["save_to_db"] = save_to_db
    return client.post("/upload", data=data, content_type="multipart/form-data")


def connected_client(make_client, fake_mysql, tmp_path):
    """A client whose application is connected to the fake MySQL server."""
    client = make_client(MYSQL_SETTINGS_FILE=str(tmp_path / "mysql.json"))
    client.post("/api/database/connect", json={"host": "db.internal", "user": "ocr"})
    return client


def test_upload_stores_the_extraction_in_mysql(
    make_client, fake_mysql, tmp_path, text_pdf_factory, review_save
):
    """The browser flow stores **after** the review, with the reviewed fields."""
    client = connected_client(make_client, fake_mysql, tmp_path)
    document = text_pdf_factory(DIGITAL_PDF_TEXT + "\nTotal: 42.00 EUR")

    review = upload_document(client, document, "digital.pdf")

    assert review.status_code == 200
    assert "nothing has been stored yet" in review.get_data(as_text=True)
    assert "Save the reviewed data to MySQL" in review.get_data(as_text=True)
    assert fake_mysql[0].records == [], "uploading alone must not write to the store"

    response = review_save(client, review, total_amount_0="128.50")
    body = response.get_data(as_text=True)


    assert response.status_code == 200
    assert "Stored in MySQL as" in body
    assert "#1" in body
    assert "128.50" in body, "the corrected value is what the page reports back"

    connection = fake_mysql[0]
    assert len(connection.records) == 1
    stored = connection.records[0]
    assert stored["filename"] == "digital.pdf"
    assert DIGITAL_PDF_TEXT[:30] in stored["content"]
    assert stored["page_count"] == 1
    assert stored["total_amount"] == "128.50", "the reviewer's correction is stored"
    assert stored["currency"] == "EUR"
    assert stored["uploaded_at"].tzinfo is None
    assert len(connection.page_rows) == 1


def test_upload_can_opt_out_of_mysql_storage(
    make_client, fake_mysql, tmp_path, text_pdf_factory, review_save
):
    client = connected_client(make_client, fake_mysql, tmp_path)
    review = upload_document(client, text_pdf_factory(DIGITAL_PDF_TEXT), "digital.pdf")

    response = review_save(client, review, save_to_db="0")
    body = response.get_data(as_text=True)


    assert response.status_code == 200
    assert "Stored in MySQL" not in body
    assert "storing was switched off" in body
    assert fake_mysql[0].records == []
    assert fake_mysql[0].page_rows == []



def test_api_ocr_saves_and_reports_the_record(
    make_client, fake_mysql, tmp_path, text_pdf_factory, upload_file
):
    client = connected_client(make_client, fake_mysql, tmp_path)

    response = upload_file(
        client, text_pdf_factory(DIGITAL_PDF_TEXT), "digital.pdf", url="/api/ocr"
    )
    payload = response.get_json()

    assert payload["database"] == {
        "connected": True,
        "saved": True,
        "record_id": 1,
        "error": None,
    }
    assert fake_mysql[0].records[0]["filename"] == "digital.pdf"


def test_extraction_survives_a_broken_database(
    make_client, fake_mysql, tmp_path, text_pdf_factory, review_save
):
    """A failing INSERT must not cost the user the text they waited for."""
    client = connected_client(make_client, fake_mysql, tmp_path)
    fake_mysql[0].fail_on = "insert into"
    review = upload_document(client, text_pdf_factory(DIGITAL_PDF_TEXT), "digital.pdf")

    response = review_save(client, review)
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "selectable text layer" in body, "the extraction is still shown"
    assert "This document was not stored" in body, "the failure is reported next to it"
    assert "simulated failure" in body
    assert fake_mysql[0].rollbacks == 1
    assert fake_mysql[0].records == []



def test_upload_without_a_connection_is_unchanged(client, text_pdf_factory):
    """The database is optional: no connection, no database noise on the page."""
    response = upload_document(client, text_pdf_factory(DIGITAL_PDF_TEXT), "digital.pdf")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "selectable text layer" in body
    assert "Saved to MySQL" not in body
    assert "storing it in MySQL failed" not in body


def test_health_reports_the_database_state(client):
    health = client.get("/api/health").get_json()["database"]

    assert health["connected"] is False
    assert health["driver_available"] is True
    assert health["record_count"] is None
    assert "password" not in json.dumps(health)
