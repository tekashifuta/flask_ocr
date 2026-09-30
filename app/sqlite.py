"""SQLite store for extracted text - the server-less sibling of MySQL.

Same two tables as :mod:`app.database` (``ocr_extractions`` plus its per-page
table), the same columns, the same behaviour - in a single file, through the
standard library's :mod:`sqlite3`.  Nothing to install, no server to run, no
credentials to type, which makes it the right store for

* **testing** - the whole storage path (saving, searching, the records view, the
  Excel export) can be exercised against a real database instead of a stub, and
* a **fallback** - ``DATABASE_BACKEND=auto`` still has a store when no MySQL
  server answers, and ``DATABASE_BACKEND=sqlite`` skips MySQL entirely.

Only the dialect differs from :mod:`app.database` - ``?`` placeholders instead of
``%s``, ``SUBSTR()`` instead of ``LEFT()``, a ``sqlite_master`` probe instead of
``information_schema``, and ``PRAGMA foreign_keys = ON`` so the page rows cascade -
which is why the settings validation, the identifier whitelist, the search clauses
and the row serialisation are *imported* from there instead of being copied.

Use it explicitly when you want it::

    create_app({"DATABASE_BACKEND": "sqlite", "SQLITE_PATH": "instance/ocr.sqlite3"})

or connect it at runtime from the Database page / ``POST /api/database/connect``
with ``{"backend": "sqlite", "path": "..."}``.  ``SQLITE_PATH=:memory:`` gives a
throw-away database: perfect for a test process, gone when it exits (and empty
again after a reconnect, which is what "in memory" means).

Timestamps are written as ``YYYY-MM-DD HH:MM:SS.ffffff`` text (always UTC): it
sorts chronologically, needs no datetime adapter (that adapter is deprecated) and
keeps the behaviour identical on every Python version.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .database import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_SCOPE,
    DEFAULT_TABLE,
    EXPORT_COLUMNS,
    EXPORT_PAGE_COLUMNS,
    EXTRACTIONS_COLUMNS,
    FIELD_COLUMNS,
    MAX_FILENAME_CHARS,
    PAGES_COLUMNS,
    PAGES_TABLE_SUFFIX,
    RECORD_PREVIEW_CHARS,
    as_utc,
    clamp_record_limit,
    clamp_record_offset,
    content_sha256,
    derived_name,
    extraction_params,
    quote_identifier,
    sanitize_identifier,
    search_clause,
    serialise_row,
    serialise_timestamp,
    utc_now,
    whole_number,
)
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

logger = logging.getLogger(__name__)

#: What the UI/API read - the MySQL store reports ``"mysql"``.
PROVIDER = "sqlite"
#: Used in the "not a valid ... name" messages of the shared identifier helpers.
DIALECT = "SQLite"
#: The standard library module that does the actual work.
DRIVER_NAME = "sqlite3"

#: SQLite's own name for a database that only lives in this process.
MEMORY_PATH = ":memory:"
#: Default file name inside the Flask instance folder.
DEFAULT_FILE_NAME = "ocr_records.sqlite3"

#: Parameter style of the driver; the clause builders are shared with MySQL.
PLACEHOLDER = "?"

#: What the record list selects: the metadata plus a bound-length preview, and the
#: full content length.  ``SUBSTR`` is SQLite's ``LEFT``.
LIST_COLUMNS = (
    "`id`, `filename`, `uploaded_at`, `kind`, `page_count`, `char_count`, "
    "`word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, "
    "`engine_version`, `stored_at`, "
    f"{FIELD_COLUMNS}, "
    "SUBSTR(`content`, 1, ?) AS `preview`, LENGTH(`content`) AS `content_chars`"
)

#: The structured fields (app/fields.py) in one file: TEXT, except the amount, which
#: is REAL so ``ORDER BY`` and a spreadsheet treat it as the number it is.
SQLITE_FIELD_COLUMN_TYPES: dict[str, str] = {
    "supplier": "TEXT",
    "invoice_number": "TEXT",
    "document_date": "TEXT",
    "total_amount": "REAL",
    "currency": "TEXT",
}



# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
def create_table_sql(table: str) -> str:
    """DDL for the parent table: one row per extraction, structured fields included.

    ``INTEGER PRIMARY KEY AUTOINCREMENT`` is SQLite's auto increment (it *is* the
    rowid, so it behaves like MySQL's ``BIGINT UNSIGNED AUTO_INCREMENT``), and
    ``stored_at`` defaults to ``CURRENT_TIMESTAMP`` - which SQLite writes in UTC.
    """
    name = quote_identifier(table, label="Table name", dialect=DIALECT)
    field_lines = "".join(
        f"\n  `{column}` {SQLITE_FIELD_COLUMN_TYPES[column]} NULL," for column in FIELD_ORDER
    )
    return f"""CREATE TABLE IF NOT EXISTS {name} (
  `id` INTEGER PRIMARY KEY AUTOINCREMENT,
  `filename` VARCHAR({MAX_FILENAME_CHARS}) NOT NULL,
  `uploaded_at` DATETIME NOT NULL,
  `content` TEXT NOT NULL,
  `kind` VARCHAR(16) NOT NULL DEFAULT 'image',
  `page_count` INTEGER NOT NULL DEFAULT 0,
  `char_count` INTEGER NOT NULL DEFAULT 0,
  `word_count` INTEGER NOT NULL DEFAULT 0,
  `confidence` REAL NULL,
  `duration_ms` INTEGER NOT NULL DEFAULT 0,
  `size_bytes` INTEGER NOT NULL DEFAULT 0,
  `ocr_language` VARCHAR(64) NULL,
  `engine_version` VARCHAR(64) NULL,{field_lines}
  `content_sha256` CHAR(64) NULL,
  `stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""



def create_pages_table_sql(pages_table: str, extractions_table: str) -> str:
    """DDL for the child table: one row per page, ``ON DELETE CASCADE``.

    SQLite only enforces the cascade when ``PRAGMA foreign_keys = ON`` is set, which
    :meth:`SqliteDatabase.connect` does on every connection.
    """
    name = quote_identifier(pages_table, label="Pages table name", dialect=DIALECT)
    parent = quote_identifier(extractions_table, label="Table name", dialect=DIALECT)
    unique_key = derived_name("uq_", pages_table, "_page")
    return f"""CREATE TABLE IF NOT EXISTS {name} (
  `id` INTEGER PRIMARY KEY AUTOINCREMENT,
  `extraction_id` INTEGER NOT NULL,
  `page_number` INTEGER NOT NULL,
  `method` VARCHAR(16) NOT NULL DEFAULT 'ocr',
  `content` TEXT NOT NULL,
  `char_count` INTEGER NOT NULL DEFAULT 0,
  `word_count` INTEGER NOT NULL DEFAULT 0,
  `confidence` REAL NULL,
  `duration_ms` INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT `{unique_key}` UNIQUE (`extraction_id`, `page_number`),
  FOREIGN KEY (`extraction_id`) REFERENCES {parent} (`id`) ON DELETE CASCADE
)"""


def create_index_sql(table: str) -> tuple[str, ...]:
    """The lookup indexes, named exactly like the ones the MySQL builder derives."""
    name = quote_identifier(table, label="Table name", dialect=DIALECT)
    columns = (
        ("_uploaded_at", "uploaded_at"),
        ("_filename", "filename"),
        ("_sha256", "content_sha256"),
    )
    return tuple(
        f"CREATE INDEX IF NOT EXISTS "
        f"{quote_identifier(derived_name('idx_', table, suffix), dialect=DIALECT)} "
        f"ON {name} (`{column}`)"
        for suffix, column in columns
    )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SqliteSettings:
    """Where the SQLite store lives: one file (or ``:memory:``), no credentials.

    :meth:`to_public_dict` mirrors
    :meth:`app.database.MySqlSettings.to_public_dict`, including the ``database``
    key that fills the "schema" slot the shared templates print, so the UI and the
    API can treat both stores the same way.
    """

    path: str = MEMORY_PATH
    table: str = DEFAULT_TABLE
    pages_table: str = f"{DEFAULT_TABLE}{PAGES_TABLE_SUFFIX}"
    #: Seconds to wait for a lock held by another connection before giving up.
    timeout: int = DEFAULT_CONNECT_TIMEOUT

    def __post_init__(self) -> None:
        if not str(self.path or "").strip():
            raise InvalidDatabaseSettingsError(
                "Enter the path of the SQLite file (for example instance/ocr.sqlite3)."
            )
        if self.table == self.pages_table:
            raise InvalidDatabaseSettingsError(
                "The per-page table must use a different name than the extractions table."
            )

    @classmethod
    def from_mapping(
        cls, data, *, defaults: "SqliteSettings | None" = None
    ) -> "SqliteSettings":
        """Build settings from a form (``MultiDict``) or a JSON object.

        Blank values fall back to *defaults*, exactly like
        :meth:`app.database.MySqlSettings.from_mapping` - including the path, so an
        empty input means "wherever the configuration points"; table names go through
        the same identifier whitelist, because they cannot be bound as parameters.
        """
        base = defaults or cls()

        def _text(key: str, fallback: str) -> str:
            raw = data.get(key)
            return fallback if raw is None else str(raw).strip()

        path = _text("path", base.path) or base.path
        table = sanitize_identifier(
            _text("table", base.table), DEFAULT_TABLE, label="Table name", dialect=DIALECT
        )
        pages_table = sanitize_identifier(
            _text("pages_table", "") or f"{table}{PAGES_TABLE_SUFFIX}",
            f"{DEFAULT_TABLE}{PAGES_TABLE_SUFFIX}",
            label="Pages table name",
            dialect=DIALECT,
        )
        return cls(
            path=path,
            table=table,
            pages_table=pages_table,
            timeout=whole_number(
                data.get("timeout"),
                base.timeout,
                label="Timeout",
                minimum=1,
                maximum=120,
            ),
        )

    @property
    def is_memory(self) -> bool:
        """True for ``:memory:`` - a database that never touches the disk."""
        return self.path == MEMORY_PATH

    @property
    def connection_label(self) -> str:
        """Where the data lives - what the UI shows in the "Server"/"File" row."""
        return self.path

    @property
    def label(self) -> str:
        """How a log line or a message names this store."""
        return f"SQLite file {self.path}"

    def to_public_dict(self) -> dict:
        """Everything the UI/API may see - there is no secret to hide here."""
        return {
            "path": self.path,
            "database": self.path,
            "table": self.table,
            "pages_table": self.pages_table,
            "timeout": self.timeout,
            "label": self.label,
        }


# ---------------------------------------------------------------------------
# row helpers
# ---------------------------------------------------------------------------
#: Columns whose text value the UI shows as ``YYYY-MM-DD HH:MM:SS UTC``.
_TIMESTAMP_COLUMNS = ("uploaded_at", "stored_at")


def stored_timestamp(value: datetime) -> str:
    """``YYYY-MM-DD HH:MM:SS.ffffff`` - the text form a DATETIME column holds.

    The value is always UTC and this form sorts chronologically as text.  Writing
    the string ourselves (instead of leaning on the module's datetime adapter, which
    is deprecated) keeps the store behaving the same on every Python version.
    """
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def clean_row(row) -> dict:
    """Serialise one row, wording timestamps the way the MySQL store does."""
    serialised = serialise_row(row)
    for column in _TIMESTAMP_COLUMNS:
        value = serialised.get(column)
        if value is not None:
            serialised[column] = f"{str(value).replace('T', ' ')[:19]} UTC"
    return serialised


def sqlite_error_types() -> tuple[type[BaseException], ...]:
    """Exception classes that mean "the file or the statement failed"."""
    return (sqlite3.Error, OSError)


def driver_version() -> str | None:
    """Version of the SQLite library (``sqlite3.sqlite_version``)."""
    return sqlite3.sqlite_version


# ---------------------------------------------------------------------------
# connection + schema
# ---------------------------------------------------------------------------
class SqliteDatabase:
    """One live ``sqlite3`` connection plus the tables it created.

    The public surface is the one :class:`app.database.MySqlDatabase` exposes, so
    :class:`app.database.DatabaseManager` and the routes treat both stores alike.

    ``sqlite3`` connections may not be shared between threads by default and Flask's
    development server is threaded, so the connection is opened with
    ``check_same_thread=False`` and every statement is serialised behind an ``RLock``
    - the same bargain the MySQL store makes.  ``isolation_level=None`` turns the
    module's implicit transactions off, so :meth:`save_extraction` drives
    ``BEGIN``/``COMMIT``/``ROLLBACK`` itself and the two inserts stay atomic.
    """

    #: Read by the UI/API instead of asking the class - the MySQL store sets it too.
    provider = PROVIDER

    def __init__(self, settings: SqliteSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self.database_created = False
        self.tables_created = False
        self.server_version: str | None = None
        self.connected_at: datetime | None = None
        self.last_error: str | None = None

    # -- connecting ------------------------------------------------------
    def connect(self) -> "SqliteDatabase":
        """Open the file (creating it, both tables and the indexes) on the way in."""
        with self._lock:
            self.close()
            existed = self._file_exists()
            try:
                self._connection = self._open()
                self.tables_created = self._create_tables()
                self.server_version = driver_version()
            except DatabaseError:
                raise
            except sqlite_error_types() as exc:
                self.last_error = str(exc)
                self.close()
                raise DatabaseUnavailableError(
                    f"Could not open the SQLite file {self.settings.path}: {exc}"
                ) from exc
            # For :memory: there is no file to have created.
            self.database_created = existed is False
            self.last_error = None
            self.connected_at = utc_now()

        logger.info(
            "SQLite ready: %s (sqlite %s, tables %s)",
            self.settings.label,
            self.server_version,
            "created" if self.tables_created else "already present",
        )
        return self

    def ensure_schema(self) -> bool:
        """Create whatever is missing on the *current* connection."""
        with self._lock:
            self._ensure_healthy()
            try:
                created = self._create_tables()
            except sqlite_error_types() as exc:
                raise self._failure("Creating the SQLite schema", exc) from exc
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
            logger.debug("Ignoring error while closing the SQLite connection: %s", exc)

    # -- low level -------------------------------------------------------
    def _resolved_path(self) -> str:
        """The path as the OS sees it (``~`` expanded); ``:memory:`` is untouched."""
        if self.settings.is_memory:
            return MEMORY_PATH
        return str(Path(self.settings.path).expanduser())

    def _file_exists(self) -> bool | None:
        """``True``/``False`` for a file path, ``None`` for ``:memory:``."""
        if self.settings.is_memory:
            return None
        return Path(self._resolved_path()).is_file()

    def _open(self) -> sqlite3.Connection:
        path = self._resolved_path()
        if not self.settings.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path,
            timeout=self.settings.timeout,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        # The page rows cascade only while foreign keys are enforced, and SQLite has
        # them off by default.
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL survives a crash better and lets readers work during a write; it is a
        # no-op for :memory:, and an exotic file system may refuse it.
        self._ignore_failure(connection, "PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _ignore_failure(connection: sqlite3.Connection, statement: str) -> None:
        try:
            connection.execute(statement)
        except sqlite3.Error as exc:  # pragma: no cover - file system dependent
            logger.debug("Ignoring %r on the SQLite connection: %s", statement, exc)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseNotConfiguredError(
                "No SQLite database is connected. Open the Database page and connect "
                "one first."
            )
        return self._connection

    def _ensure_healthy(self) -> sqlite3.Connection:
        """Return a usable connection, or report that the handle went stale."""
        connection = self._require_connection()
        try:
            connection.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - any driver error means "unusable"
            self.last_error = str(exc)
            raise DatabaseUnavailableError(
                f"The SQLite connection to {self.settings.path} was lost: {exc}"
            ) from exc
        return connection

    def _failure(self, action: str, exc: Exception) -> DatabaseError:
        self.last_error = str(exc)
        return DatabaseWriteError(f"{action} failed on {self.settings.path}: {exc}")

    def _execute(self, sql: str, params=None) -> int:
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params or ())
                return cursor.rowcount
            finally:
                cursor.close()

    def _fetchone(self, sql: str, params=None):
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params or ())
                return cursor.fetchone()
            finally:
                cursor.close()

    def _fetchall(self, sql: str, params=None) -> list:
        with self._lock:
            cursor = self._require_connection().cursor()
            try:
                cursor.execute(sql, params or ())
                return list(cursor.fetchall())
            finally:
                cursor.close()

    def _table(self) -> str:
        """Quoted name of the extractions table."""
        return quote_identifier(self.settings.table, dialect=DIALECT)

    def _pages_table(self) -> str:
        """Quoted name of the per-page table."""
        return quote_identifier(self.settings.pages_table, dialect=DIALECT)

    # -- schema ----------------------------------------------------------
    def _create_tables(self) -> bool:
        """Create the tables and indexes when they are missing.

        ``True`` when *this* call created a table - the same contract the MySQL store
        offers, so the status panel reads the same for both.
        """
        created = False
        statements = (
            (self.settings.table, create_table_sql(self.settings.table)),
            (
                self.settings.pages_table,
                create_pages_table_sql(self.settings.pages_table, self.settings.table),
            ),
        )
        for name, statement in statements:
            existed = self._table_exists(name)
            self._execute(statement)
            if existed is False:
                created = True
        for statement in create_index_sql(self.settings.table):
            self._execute(statement)
        self._add_field_columns()
        return created

    def _table_columns(self, table: str) -> set[str]:
        """The column names of *table*, straight from ``PRAGMA table_info``."""
        try:
            rows = self._fetchall(f"PRAGMA table_info({quote_identifier(table, dialect=DIALECT)})")
        except sqlite_error_types() as exc:
            logger.debug("Column probe for %s failed (%s); assuming none", table, exc)
            return set()
        names = set()
        for row in rows:
            try:
                names.add(str(row["name"]))
            except (KeyError, IndexError, TypeError):
                names.add(str(row[1]))
        return names

    def _add_field_columns(self, table: str | None = None) -> tuple[str, ...]:
        """Add the structured field columns to a file created before they existed.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        database written by an older version of this application would keep failing
        every save with "no such column".  ``PRAGMA table_info`` tells us what is
        there and ``ALTER TABLE ADD COLUMN`` adds the rest - in place, without
        touching a single stored row (the older rows simply hold ``NULL`` fields).
        """
        target = table or self.settings.table
        existing = self._table_columns(target)
        if not existing:
            return ()
        missing = tuple(name for name in FIELD_ORDER if name not in existing)
        if not missing:
            return ()
        name = quote_identifier(target, dialect=DIALECT)
        for column in missing:
            self._execute(
                f"ALTER TABLE {name} ADD COLUMN `{column}` "
                f"{SQLITE_FIELD_COLUMN_TYPES[column]} NULL"
            )
        logger.info("Added %s structured field column(s) to %s", len(missing), target)
        return missing


    def _table_exists(self, table: str) -> bool | None:
        """``True``/``False``, or ``None`` when the file would not tell us."""
        try:
            row = self._fetchone(
                "SELECT `name` FROM `sqlite_master` WHERE `type` = 'table' AND `name` = ?",
                (table,),
            )
        except sqlite_error_types() as exc:
            logger.debug("Table probe for %s failed (%s); assuming it is new", table, exc)
            return None
        return row is not None

    # -- writes ----------------------------------------------------------
    def save_extraction(
        self,
        result: ExtractionResult,
        *,
        uploaded_at: datetime | None = None,
        fields: DocumentFields | None = None,
    ) -> int:
        """Insert one extraction (plus its pages) and return the new record id.

        Both inserts share a transaction, so a failure leaves no half-written record
        behind.  ``uploaded_at`` defaults to "now" in UTC.  *fields* are the
        **reviewed** structured values; without it the ones the parser proposed are
        stored (see :func:`app.database.resolve_fields`).
        """
        content = result.full_text()
        timestamp = stored_timestamp(as_utc(uploaded_at))
        columns = ", ".join(f"`{name}`" for name in EXTRACTIONS_COLUMNS)
        placeholders = ", ".join([PLACEHOLDER] * len(EXTRACTIONS_COLUMNS))
        sql = f"INSERT INTO {self._table()} ({columns}) VALUES ({placeholders})"
        params = extraction_params(result, content, timestamp, fields)


        with self._lock:
            connection = self._ensure_healthy()
            cursor = connection.cursor()
            try:
                cursor.execute("BEGIN")
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
        placeholders = ", ".join([PLACEHOLDER] * (len(PAGES_COLUMNS) + 1))
        sql = (
            f"INSERT INTO {self._pages_table()} ({columns}) VALUES ({placeholders})"
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
        with self._lock:
            self._ensure_healthy()
            try:
                removed = self._execute(
                    f"DELETE FROM {self._table()} WHERE `id` = ?", (int(record_id),)
                )
            except sqlite_error_types() as exc:
                raise self._failure("Deleting the stored extraction", exc) from exc
        return bool(removed)

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except Exception as exc:  # noqa: BLE001 - the original error matters more
            logger.debug("Rollback failed: %s", exc)

    # -- reads -----------------------------------------------------------
    def record_count(self) -> int:
        """How many extractions are stored (used by the status panel)."""
        with self._lock:
            self._ensure_healthy()
            try:
                row = self._fetchone(f"SELECT COUNT(*) AS `total` FROM {self._table()}")
            except sqlite_error_types() as exc:
                raise self._failure("Counting the stored extractions", exc) from exc
        return int(row["total"] or 0) if row is not None else 0

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

        The semantics are the MySQL store's, to the letter: the term is matched
        case-insensitively (SQLite's ``LIKE`` is case-insensitive for ASCII), may
        contain ``%``/``_``/``!`` without turning into wildcards, and a purely
        numeric term also finds that record number.  *offset* is the ``OFFSET`` of
        that one query, i.e. the record the requested page starts at.
        """
        where, params = search_clause(query, scope, placeholder=PLACEHOLDER)
        return self._list_extractions(where, params, limit, offset=offset)

    def count_extractions(
        self, query: object = "", *, scope: object = DEFAULT_SEARCH_SCOPE
    ) -> int:
        """How many records match *query* - the row count behind the page buttons.

        It runs the very same :func:`search_clause` as :meth:`search_extractions`,
        so a page count can never disagree with the rows it is counting.
        """
        where, params = search_clause(query, scope, placeholder=PLACEHOLDER)
        with self._lock:
            self._ensure_healthy()
            try:
                row = self._fetchone(
                    f"SELECT COUNT(*) AS `total` FROM {self._table()}{where}", params
                )
            except sqlite_error_types() as exc:
                raise self._failure("Counting the matching extractions", exc) from exc
        return int(row["total"] or 0) if row is not None else 0

    def export_extractions(
        self,
        query: object = "",
        limit: object = DEFAULT_LIST_LIMIT,
        *,
        scope: object = DEFAULT_SEARCH_SCOPE,
        offset: object = 0,
    ) -> list[dict]:
        """The rows of the Excel export: the same search, with the whole text."""
        where, params = search_clause(query, scope, placeholder=PLACEHOLDER)
        return self._list_extractions(
            where, params, limit, offset=offset, columns=EXPORT_COLUMNS, preview_chars=None
        )

    def pages_for_extractions(self, record_ids: Sequence[int]) -> list[dict]:
        """Every stored page of the given records, one query, ordered by record/page."""
        identifiers = [int(value) for value in record_ids]
        if not identifiers:
            return []
        placeholders = ", ".join([PLACEHOLDER] * len(identifiers))
        sql = (
            f"SELECT {EXPORT_PAGE_COLUMNS} FROM {self._pages_table()} "
            f"WHERE `extraction_id` IN ({placeholders}) "
            "ORDER BY `extraction_id` ASC, `page_number` ASC"
        )
        with self._lock:
            self._ensure_healthy()
            try:
                rows = self._fetchall(sql, tuple(identifiers))
            except sqlite_error_types() as exc:
                raise self._failure("Loading the stored pages", exc) from exc
        return [clean_row(row) for row in rows]

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
        sql = (
            f"SELECT {columns} FROM {self._table()}{where} "
            "ORDER BY `uploaded_at` DESC, `id` DESC LIMIT ?"
        )
        bound = (*((preview_chars,) if preview_chars is not None else ()), *params, safe_limit)
        if safe_offset:
            sql += " OFFSET ?"
            bound = (*bound, safe_offset)
        with self._lock:
            self._ensure_healthy()
            try:
                rows = self._fetchall(sql, bound)
            except sqlite_error_types() as exc:
                raise self._failure("Listing the stored extractions", exc) from exc
        return [clean_row(row) for row in rows]

    def get_extraction(self, record_id: int) -> dict | None:
        """One record including the full text and its per-page rows."""
        identifier = int(record_id)
        with self._lock:
            self._ensure_healthy()
            try:
                parent = self._fetchone(
                    f"SELECT * FROM {self._table()} WHERE `id` = ?", (identifier,)
                )
                if parent is None:
                    return None
                pages = self._fetchall(
                    "SELECT `page_number`, `method`, `content`, `char_count`, `word_count`, "
                    f"`confidence`, `duration_ms` FROM {self._pages_table()} "
                    "WHERE `extraction_id` = ? ORDER BY `page_number` ASC",
                    (identifier,),
                )
            except sqlite_error_types() as exc:
                raise self._failure("Loading the stored extraction", exc) from exc
        record = clean_row(parent)
        record["pages"] = [clean_row(page) for page in pages]
        return record

    def require_extraction(self, record_id: int) -> dict:
        """Like :meth:`get_extraction`, but raises the 404 the UI wants."""
        record = self.get_extraction(record_id)
        if record is None:
            raise DatabaseRecordNotFoundError(
                f"Record #{int(record_id)} is not stored in "
                f"{self.settings.path} ({self.settings.table})."
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




