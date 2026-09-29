# SQLite storage: the same tables without a MySQL server

Everything is complete and verified. Here's the summary.

## What was added

### New module — `app/sqlite.py` (~800 lines)

The **same two tables** as the MySQL store (`ocr_extractions` + its per-page table,
identical columns) in **one file**, through the standard library's `sqlite3`:
nothing to install, no server, no credentials. It exists so the storage path can be
**tested** (and used) on a machine without MySQL, and so `DATABASE_BACKEND=auto` has
something to fall back to at start-up.

* **`SqliteSettings(path, table, pages_table, timeout)`** — mirrors `MySqlSettings`
  (`from_mapping` over a form/JSON body, `to_public_dict`, `connection_label`,
  `is_memory`, `:memory:` for a throw-away database; table names go through the same
  identifier whitelist). `to_public_dict()` fills the `database` slot with the file
  path so the shared templates keep working.
* **DDL** — `create_table_sql` / `create_pages_table_sql` / `create_index_sql`:
  `INTEGER PRIMARY KEY AUTOINCREMENT`, `TIMESTAMP ... DEFAULT CURRENT_TIMESTAMP`,
  the same `UNIQUE (extraction_id, page_number)` and
  `FOREIGN KEY ... ON DELETE CASCADE`, and indexes named exactly like the MySQL ones
  (`idx_<table>_uploaded_at` / `_filename` / `_sha256`, via the shared `derived_name`).
* **`SqliteDatabase`** — method for method the surface of `MySqlDatabase`: `connect`
  (creates the file, its parent folder, the tables and the indexes; reports
  `database_created`/`tables_created`/`server_version`), `ensure_schema`, `close`,
  `save_extraction` (parent + page rows in **one** `BEGIN`/`COMMIT`, rollback on
  failure), `delete_extraction`, `record_count`, `recent_extractions`,
  `search_extractions`, `export_extractions`, `pages_for_extractions`,
  `get_extraction`, `require_extraction`, `status`.
* **No dialect duplication.** `?` placeholders instead of `%s`, `SUBSTR()` instead of
  `LEFT()`, a `sqlite_master` probe, `PRAGMA foreign_keys = ON` (cascades are off by
  default in SQLite) and `PRAGMA journal_mode = WAL` — while the column lists, the
  identifier helpers, `search_clause(..., placeholder="?")`, `clamp_record_limit`,
  `content_sha256`, `as_utc` and `serialise_row` are **imported from**
  `app/database.py`.
* **Timestamps** are written as `YYYY-MM-DD HH:MM:SS.ffffff` UTC text (sortable, and
  no reliance on the deprecated `sqlite3` datetime adapter); `clean_row()` renders
  them as the familiar `… UTC` the MySQL rows show.
* One connection behind an `RLock` with `check_same_thread=False` and
  `isolation_level=None`, so the Flask dev server can stay threaded and
  `save_extraction` owns its transaction.

### Shared layer — `app/database.py`

* New **backend registry**: `BACKEND_AUTO`/`BACKEND_MYSQL`/`BACKEND_SQLITE`,
  `Backend` (id + the UI nouns), `STORES`, `normalize_backend()` and
  `backend_labels()` — `auto` is presented as MySQL while nothing is connected.
* `DatabaseManager` now owns **which store is live**: `connect(data, backend=)`
  dispatches on the `backend` field (default: `DATABASE_BACKEND`), `connect_mysql`,
  **`connect_sqlite`** (remembers the path in-process for the next form),
  **`auto_connect()`** (MySQL, falling back to the file in `auto` mode), `labels`,
  `backend`/`configured_backend`, and a richer `status()` that keeps its old keys and
  adds `backend` + `backends` (both stores, driver state, SQLite path).
  `require_database()` speaks about the store that is configured.
* Reusable, no longer private: `EXTRACTIONS_COLUMNS`, `PAGES_COLUMNS`, `LIST_COLUMNS`,
  `EXPORT_COLUMNS`, `EXPORT_PAGE_COLUMNS`, `serialise_row`, `serialise_timestamp`,
  `derived_name`, `whole_number`, `as_utc`; `sanitize_identifier`/`quote_identifier`
  take a `dialect` (their messages say "valid SQLite name" for that store) and
  `search_clause` takes a `placeholder`. MySQL behaviour is unchanged.

### Configuration — `app/config.py`

`DATABASE_BACKEND` (`auto`/`mysql`/`sqlite`, validated by a new `_env_choice`),
`DATABASE_AUTO_CONNECT`, `DATABASE_AUTO_SAVE`, `SQLITE_PATH`, `SQLITE_TABLE`,
`SQLITE_TIMEOUT`. `MYSQL_AUTO_CONNECT`/`MYSQL_AUTO_SAVE` keep working as the older
names (they are read as the default of the new pair), so nothing existing has to
change.

### HTTP layer — `app/routes.py`

* `POST /database/connect` and `POST /api/database/connect` accept
  `backend`/`path` (plus the table names) next to the MySQL fields, and
  `_schema_message()` now builds its wording from the status payload, so the page and
  the API describe whatever was connected ("Created the SQLite file '…'" /
  "Created the schema '…'").
* `_should_save_to_database` reads `DATABASE_AUTO_SAVE` (falling back to
  `MYSQL_AUTO_SAVE`), `_persist` and the log lines name the live store, the file name
  echoed back after a failed connect includes the SQLite `path`, `GET /api/health`
  reports `backend` + `sqlite_available`, and the index page gets the full status
  payload (so "Stored records" shows a number instead of a blank).

### UI — templates

A context processor exposes the live store to every page as `store`
(`label`/`server_term`/`server_phrase`), so nothing says "MySQL" while SQLite is
connected — with `DATABASE_BACKEND=auto` and nothing connected it is MySQL, i.e. the
existing wording is unchanged. `/database` gained a **Use a local SQLite file** panel
(hidden `backend=sqlite`, pre-filled path, hint) and shows store/driver/version/file
rows plus the SQLite path the fallback would use; `_records.html`, `result.html`,
`record.html`, `records.html`, `index.html` and the footer follow the same labels.

### Tests — new `tests/test_sqlite.py` (39 tests)

**Full suite 155 → 194, all passing.** Unlike `tests/test_database.py` (a fake
PyMySQL connection), these talk to a **real** database file in `tmp_path`: settings
and DDL, creating the file/tables/indexes, reconnecting, recreating a dropped table,
saving with the exact timestamp/pages/statistics, the UTC-now default, a
**whole-transaction rollback** (a duplicate page number breaks the `UNIQUE` key), the
cascading delete, the shared search semantics (`%`/`_`/`!` literal, scopes, record
numbers, blank term, clamped limit), the export columns, the one-query pages read,
`clean_row`/`stored_timestamp`, the status payload, the manager's backend choice, the
start-up fallback and the whole HTTP path — connect via the form **and** the JSON API,
upload, records view, search, record page, `.xlsx` export, delete, the `400` for bad
settings and the "driver missing" page that still offers SQLite.

**Verified live:** through a test client against a real file — connect twice
("Created the SQLite file …" then "The file and its tables were already in place."),
upload a text-layer PDF, search, open the record, export both workbooks, delete, and
`/api/health` reporting `backend`/`provider`/`sqlite_available`. A real MySQL connect
attempt on this machine still fails with the usual `503` page, which now also offers
the SQLite panel as the way forward.

**Limitations to note:** SQLite is a single-file database — one writer at a time
(hence the busy `timeout` and WAL), and no user/permission model beyond the file
system, which is exactly why it is documented as the test / local / default-`auto`
store rather than a replacement for MySQL. `auto` only falls back at *start-up*; a
**connect** a human submitted reports its error, so a typo cannot silently move the
data into another store.
