Everything is complete and verified. Here's the summary.

## What was added: MySQL storage for extracted data

### New module — `app/database.py` (~1050 lines)
A self-contained PyMySQL layer (pure-Python driver, no compiler needed):

* **`MySqlSettings`** – validated host/port/user/password/database/table/charset. Table/database names are **whitelisted** (1–64 chars, letters/digits/underscores) instead of escaped, because MySQL cannot bind identifiers; `to_public_dict()` never exposes the password.
* **`create_database_sql` / `create_table_sql` / `create_pages_table_sql`** – the DDL builders (`CREATE DATABASE IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`). Index/constraint names are derived with a hash suffix so a 64-char table name can't trigger MySQL error 1059.
* **`MySqlDatabase`** – one connection behind an `RLock` (PyMySQL connections aren't thread-safe) with `DictCursor`, `ping(reconnect=True)` recovery, schema creation on connect, and:
  * `save_extraction(result, uploaded_at=…)` → parent row + one page row per page in **a single transaction** (rollback on failure), returns the new record id;
  * `record_count`, `recent_extractions`, `get_extraction`, `require_extraction`, `delete_extraction`, `ensure_schema`, `status`.
* **`DatabaseManager`** – holds the *active* connection plus the optional credential memory (`instance/mysql_connection.json`, chmod 600 on POSIX, never served). A failed reconnect **keeps the working connection**.

### Schema (created automatically when you connect)
| `ocr_extractions` (1 row per upload) | `ocr_extraction_pages` (1 row per page) |
|---|---|
| `id`, **`filename`**, **`uploaded_at`** UTC `DATETIME(6)`, **`content`** `LONGTEXT`, `kind`, `page_count`, `char_count`, `word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, `engine_version`, `content_sha256`, `stored_at` + indexes | `extraction_id` → `FOREIGN KEY … ON DELETE CASCADE`, `page_number`, `method` (`ocr`/`embedded`), `content`, counts, confidence, duration |

### Connection UI + API
* `/database` – connection form (host, port, user, password, schema, tables, charset, timeout, "remember"), live status panel (driver + version, MySQL version, schema/tables, row count, saved-details file), and a **stored-records table** with Open / `.txt` / Delete. Buttons: *Connect & create schema*, *Create missing tables*, *Disconnect*, *Forget saved details*.
* `/database/records/<id>` (+ `/download`, `/delete`) – one stored extraction with its stored pages.
* JSON API: `GET /api/database`, `POST /api/database/connect|disconnect`, `GET /api/database/records[?limit=]`, `GET|DELETE /api/database/records/<id>`; `/api/health` now reports the database state.
* Uploads save automatically while connected (checkbox on the upload form, `MYSQL_AUTO_SAVE`, opt-out per upload); the result page links to the record, and a DB failure shows a warning **without losing the extraction**.
* `MYSQL_AUTO_CONNECT=1` connects and creates the schema at start-up (logged, never fatal).

### Tests & docs
`tests/test_database.py` — **62 new tests, all passing** with **no MySQL server**: a small fake of the PyMySQL surface records every statement, so the DDL, create-if-missing logic, INSERT parameters (name/UTC timestamp/content/page rows), row mapping, credential persistence, UI, JSON API and the "database is down" path are asserted. Full suite: **109 passed**. README gained a "Storing extractions in MySQL" section, endpoint/config/layout/tests/troubleshooting updates; `.gitignore` added `*.db.txt`.

**Verified live:** real driver through the HTTP stack — `POST /api/database/connect` → `503 {"code":"database_unavailable","message":"Could not connect to MySQL at root@127.0.0.1:3306/flask_ocr: (2003, …)"}`, and the browser path renders the same message with the form preserved (password never echoed). Every page/endpoint was smoke-tested (200/303/400/405 as designed).

**Limitation to note:** no MySQL server exists on this machine (only SQL Server Express) and no Docker, so the SQL was validated against the recording fake driver rather than a live server — the DDL is standard InnoDB/utf8mb4 and uses only `IF NOT EXISTS` statements, but do run one connect against your server as the final acceptance check.