# Flask OCR - text extraction from images and PDFs

A small Flask service that extracts text from **JPG/PNG images** and **PDF
documents** - including scanned pages and multi-page PDFs - using **Tesseract**
as the OCR engine. It ships a browser UI (drag & drop, per-page results, copy and
download) plus a JSON API for automation.

| | |
|---|---|
| Web UI | `POST /upload` -> per-page text, confidence, `.txt` download |
| JSON API | `POST /api/ocr` -> structured result, `GET /api/health` -> engine status |
| OCR engine | Tesseract 5.x through `pytesseract` |
| PDF handling | PDFium (`pypdfium2`) - renders pages *and* reads text layers, **no Poppler needed** |
| Storage | Extracted text optionally in **MySQL** or a **SQLite** file - connect from `/database`, schema and tables are created for you |
| Excel export | The stored records (search included) download as `.xlsx` - no extra dependency, nothing written to disk |
| Verified on | Python 3.14.6 / Windows, Tesseract 5.4.0, Flask 3.1.3, Pillow 12.3.0, pypdfium2 5.13.0, PyMySQL 1.2.3 |
| Privacy | Documents are processed **in memory** and never written to disk |

## 1. Install Tesseract

Tesseract is a native binary, not a Python package.

```powershell
# Windows (verified: installs Tesseract 5.4.0 with the English language pack)
winget install -e --id UB-Mannheim.TesseractOCR
```

```bash
# macOS
brew install tesseract

# Debian / Ubuntu
sudo apt install tesseract-ocr
```

Extra languages: install the matching `*.traineddata` into the `tessdata`
folder next to the binary, then list them in `OCR_LANGUAGES` (e.g. `eng+deu`).

The application looks for the binary in this order, so a missing `PATH` entry is
not a problem:

1. the `TESSERACT_CMD` environment variable,
2. `tesseract` on `PATH`,
3. `C:\Program Files\Tesseract-OCR\tesseract.exe` (and the usual macOS/Linux paths).

## 2. Create the environment and install dependencies

```powershell
cd d:\Python_Projects\flask_ocr
py -3.14 -m venv env                      # already present in this checkout
env\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` also installs **PyMySQL** (pure Python, no compiler) for the
optional MySQL storage described in §5. The application starts without a MySQL
server - the `/database` page simply reports "not connected", and the same page can
open a **SQLite file** instead (the `sqlite3` module ships with Python, so that
store needs no server and no installation at all).

## 3. Run it

```powershell
cd d:\Python_Projects\flask_ocr
env\Scripts\python.exe run.py             # http://127.0.0.1:5000
env\Scripts\python.exe run.py --port 8001 --debug
```

Start-up logs make the engine state obvious - including **which store the extracted
text goes into**, because that is what fills the records view:

```
app: Tesseract OCR ready: C:\Program Files\Tesseract-OCR\tesseract.exe (languages=eng, psm=3, oem=3)
app: sqlite ready: d:\Python_Projects\flask_ocr\instance\ocr_records.sqlite3 (schema already present, tables already present)
__main__: Storing extractions in SQLite (d:\Python_Projects\flask_ocr\instance\ocr_records.sqlite3) - browse them at /database/records
```

`run.py` **connects a store before it takes traffic** (the local SQLite file unless
MySQL is configured and reachable - see `DATABASE_BACKEND`), so what you extract is
really saved and the records view has something to show. Set `DATABASE_AUTO_CONNECT=0`
to connect by hand from `/database` instead; the log line then warns that uploads are
only kept in memory.

If Tesseract cannot be found you get an ERROR log line, a clear message on the
upload page, and `GET /api/health` answers `503` so a deployment check can fail
fast. Visit <http://127.0.0.1:5000/api/health> to see the resolved path, version
and installed languages.

## 4. How extraction works

```
upload ──> validate ──> kind? ─┬─ image ─> load (verify, EXIF rotate, RGB)
                               │           preprocess (grey, autocontrast, upscale)
                               │           tesseract --oem 3 --psm 3 -> text + confidence
                               │
                               └─ pdf ───> PDFium opens the document
                                           for each page:
                                             embedded text layer >= MIN_EMBEDDED_TEXT_CHARS ?
                                               yes -> use it   (fast, exact, no OCR)
                                               no  -> render at OCR_DPI -> preprocess -> OCR
                                           join pages with "----- Page n of m -----"
```

* **Images** are always OCR'd. A two-pass `verify()`/reopen check rejects
  truncated downloads, the orientation EXIF tag is honoured, and images smaller
  than `OCR_MIN_TARGET_PX` are upscaled (capped by `OCR_MAX_UPSCALE`) because
  Tesseract needs roughly 300 DPI text.
* **Scanned PDFs** (no text layer) are rasterised page by page at `OCR_DPI` and
  OCR'd - multi-page documents are walked completely.
* **Digital PDFs** are read straight from their text layer, so a 50-page report
  costs milliseconds instead of minutes. Mixed documents work too: the decision
  is made per page.
* **Safety valves**: `MAX_PDF_PAGES`, `MAX_UPLOAD_MB`, a render pixel budget
  (`OCR_MAX_RENDER_PIXELS`) and an image pixel budget (`MAX_IMAGE_PIXELS`) keep a
  hostile or accidental upload from exhausting memory. Each OCR run is bounded by
  `OCR_TIMEOUT_SECONDS`.

Validation happens in two stages: the extension is checked first, then the real
file signature (`%PDF-` magic bytes, Pillow format sniffing). A renamed file is
therefore rejected with `400` instead of being fed to the OCR engine.

## 5. Storing extractions in MySQL or SQLite

Extracted data can be persisted in a **relational database** so it survives a
restart and can be queried with plain SQL. The feature is optional and stays
dormant until you connect a store, and it never blocks the pipeline: if the
database is unreachable the text is still extracted and shown, with a warning
next to it instead of an error page.

Two stores implement the same two tables, and `DATABASE_BACKEND` picks which one
`/database` connects by default (`auto` = MySQL when it is there, SQLite as the
fallback at start-up):

| Backend | What it is | Use it for |
|---|---|---|
| `mysql` | A server, described by the `MYSQL_*` settings | Anything shared or permanent |
| `sqlite` | One file (`instance/ocr_records.sqlite3` by default), no server, no credentials | **Tests**, demos and machines without MySQL |

Connect from the UI - <http://127.0.0.1:5000/database>: fill in host, port, user,
password and the schema/table names (pre-filled from `MYSQL_*`, see §7) and submit
**Connect & create schema**. On connect the schema is created when it does not
exist:

```sql
CREATE DATABASE IF NOT EXISTS `flask_ocr` CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `ocr_extractions` ( ... );       -- one row per upload
CREATE TABLE IF NOT EXISTS `ocr_extraction_pages` ( ... );  -- one row per page
```

The MySQL user therefore needs `CREATE` on the server; a user with only
`SELECT`/`INSERT` also works when the schema has been created by someone else.
Connecting again is safe, and **Create missing tables** re-runs just the two
`CREATE TABLE` statements.

### SQLite: the same tables without a server

The second store is a single file, created on connect through Python's own
`sqlite3`: **nothing to install, no credentials, no server**. That is what the
`sqlite` backend and the *Use a local SQLite file* panel on `/database` open, and
it is the store to use for tests and local work:

```powershell
$env:DATABASE_BACKEND = "sqlite"
$env:SQLITE_PATH = "instance/test.sqlite3"     # :memory: works too
env\Scripts\python.exe run.py
```

| | |
|---|---|
| Tables | `ocr_extractions` and `ocr_extraction_pages` - the same columns as MySQL, one row per upload / per page |
| Created by | `CREATE TABLE IF NOT EXISTS` on connect plus three indexes; `PRAGMA foreign_keys = ON` makes the page rows cascade on delete |
| Path | `SQLITE_PATH`, otherwise `instance/ocr_records.sqlite3` (git-ignored); `:memory:` gives a throw-away database |
| Timestamps | `YYYY-MM-DD HH:MM:SS.ffffff` **UTC** text - sortable, and no deprecated datetime adapter involved |
| Fallback | With `DATABASE_BACKEND=auto`, start-up uses the file when MySQL is not reachable |

Everything else is shared with the MySQL store: the same search (`LIKE ... ESCAPE
'!'` with bound patterns - case-insensitive, `%`/`_` typed by a user match
literally, a numeric term also finds that record number), the same 200-row cap, the
same records view and the same Excel export. Without the
UI:

```bash
curl -X POST http://127.0.0.1:5000/api/database/connect \
     -H 'Content-Type: application/json' \
     -d '{"backend":"sqlite","path":"instance/test.sqlite3"}'
```

Once connected, every extraction is stored and the result page says so, e.g.
*Saved to MySQL as record #12* (or *Saved to SQLite as ...*). The upload form
carries a **Save the extracted data to ...** checkbox (ticked by default); unticking
it keeps that upload out of the store (the text is still extracted and shown).
`POST /api/ocr` stores as well - see `DATABASE_AUTO_SAVE` and the `save_to_db`
field in §7.

The form posts `save_to_db` **twice**: a hidden `0` (so an unticked box still sends
something) and then the checkbox itself (`1` when ticked). The route reads them all
(`request.form.getlist`) and *any* truthy value means "save" - reading only the first
value would always find the hidden `0` and store nothing. A request that sends no
field at all (the JSON API) keeps the `DATABASE_AUTO_SAVE` default.

### What is stored

`ocr_extractions` - one row per upload:

| Column | Meaning |
|---|---|
| `id` | Auto increment primary key |
| `filename` | Sanitised original file name |
| `uploaded_at` | Upload date and time, **UTC**, microseconds (`DATETIME(6)`) |
| `content` | The extracted text (`LONGTEXT`, multi-page results keep page markers) |
| `kind`, `page_count`, `char_count`, `word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, `engine_version` | The statistics the result page shows |
| `content_sha256` | SHA-256 of `content` - handy for de-duplicating |
| `stored_at` | When the row was written (`TIMESTAMP`) |

`ocr_extraction_pages` - one row per page (`extraction_id`, `page_number`,
`method` = `ocr`/`embedded`, `content`, counts, confidence, duration) with
`FOREIGN KEY ... ON DELETE CASCADE`, so deleting an extraction removes its pages.

The `/database` page lists the stored records (every one of them by default; id, file
name, upload time, pages,
characters, confidence, a text snippet) with links to re-open the text, download it
as `.txt` or delete the row. Deleting returns to the list you came from, with the
search still applied.

### Records view - browsing and searching what is stored

`/database/records` is the **records view**: the same table without the connection
form, plus a search box. The search runs **in the database** (never in Python), so
it works on any number of stored rows:

* the term is matched **case-insensitively** against the file name and the stored
  text (a "search in" selector narrows it to *file name only* or *extracted text only*);
* a **purely numeric term also matches the record id** - `42` finds record #42;
* `%`, `_` and the escape character `!` typed by a user are **escaped**, so they
  match literally instead of widening the search (`report_1` does not match
  `report-1000`), and the pattern is always a **bound parameter**
  (`LIKE %s ESCAPE '!'`) - nothing is interpolated into the SQL;
* the term is trimmed/collapsed and truncated to 120 characters, and `?limit=` is
  clamped to 200 (`MAX_LIST_LIMIT`), so a crafted URL cannot ask for a
  full table scan.

#### Every stored record on one page

A store that is not empty is shown **in full**: the default page size is the cap, so
all 23 records (or 200 of them) are in one table, and the summary line reads
"All 23 stored records." Nothing has to be clicked to see the rest of a small store.

**Rows per page** in the toolbar keeps the choice explicit - `All (up to 200)`, or
10 / 25 / 50 / 100 rows - and a store holding more than 200 records pages at that cap
(`MYSQL_RECORDS_LIMIT` sets the default; the shipped `200` means "all"). A search
lists every match the same way.

#### Paging

When fewer rows are shown than the store holds (a chosen page size, or a store bigger
than the 200 row cap), **Previous**/**Next** plus the surrounding page numbers walk
the rest - a 400 page result shows `1 2 3 4 5 … 400`, not 400 links.

* `?page=` is 1-based; a page past the end (or `0`, a negative number, junk) lands
  on the **last**/**first** page instead of an error or an empty table;
* the match total comes from the **same search criteria** (`COUNT(*)` with the same
  bound `LIKE` clause), so the summary line ("Showing rows 11-20 of 340 stored
  records (page 2 of 34)") and the page count can never disagree with the rows;
* each page is **one** `LIMIT`/`OFFSET` query - `OFFSET` is only added from page 2
  on, and it is bound like every other parameter;
* the filters live in the URL, which makes a search bookmarkable and shareable:
  `/database/records?q=invoice&scope=filename&limit=50&page=3`. The `/database` page
  uses the same table (its page buttons stay on `/database`) and accepts the same
  `?q=`/`?scope=`/`?limit=`/`?page=` parameters.

### Excel export (.xlsx)

**Export .xlsx**, next to the search box, downloads the rows the table is showing -
the current search, scope, row limit and page are applied - as an Excel workbook:

| Sheet | Contents |
|---|---|
| `Records` | One row per stored extraction: id, file name, upload time (UTC), type, pages, characters, words, confidence, duration, size, language, engine, storage time, SHA-256 and the **full extracted text** (the table itself only lists a snippet) |
| `Pages` | One row per page of those records - the `ocr_extraction_pages` rows, so the per-page result survives the export. Left out when there are none |
| `Export` | Which filters produced the file (term, scope, row limit, page, server, schema/table, row count), so a spreadsheet that travels by e-mail explains itself |

The header row is frozen and filterable, columns are sized to their content, numbers
stay numbers (counts get a thousands separator) and text stays text - a file name
like `123.pdf` is never turned into a number.

* `GET /database/records/export.xlsx?q=&scope=&limit=&page=` - the same filters and
  page as the view, and the same 200 row cap: the export is exactly what you are
  looking at (page by page).
* `GET /database/records/<id>/export.xlsx` - one record with its pages
  (**Download .xlsx** on the record page).
* Both are offered by `/database` and `/database/records`; without a connection the
  URL answers the same clear `400` as the other database endpoints.
* The workbook is written with the **standard library only** (`zipfile` + `xml`): no
  pandas, no openpyxl, nothing to install - and it is generated **in memory**, so
  the privacy story of the rest of the application is unchanged.

```bash
curl -OJ 'http://127.0.0.1:5000/database/records/export.xlsx?q=invoice&scope=filename'
```

### Remembering the connection

With **Remember these details** ticked, the MySQL credentials are written to
`instance/mysql_connection.json` (git-ignored, mode `600` on POSIX, never served
to the browser - the password is not echoed back into the form either).
**Forget saved details** deletes the file, **Disconnect** only closes the
connection. The SQLite path needs no such file: it is not a secret, so the panel
simply keeps the last connected path for the current process (set `SQLITE_PATH` to
make a location permanent).

### Without the UI

```bash
# connect - the schema and tables are created when they are missing
curl -X POST http://127.0.0.1:5000/api/database/connect \
     -H 'Content-Type: application/json' \
     -d '{"host":"db.internal","port":3306,"user":"ocr","password":"secret","database":"flask_ocr","remember":false}'

# or the local SQLite file instead of any server
curl -X POST http://127.0.0.1:5000/api/database/connect \
     -H 'Content-Type: application/json' \
     -d '{"backend":"sqlite","path":"instance/test.sqlite3"}'

# what is stored, then one record including its pages
curl 'http://127.0.0.1:5000/api/database/records?limit=5'

# paging: page 2 of the newest first listing, narrowed to 10 rows at a time
curl 'http://127.0.0.1:5000/api/database/records?limit=10&page=2'
# the whole store (no ?limit=): up to the 200-row cap
curl 'http://127.0.0.1:5000/api/database/records'

# searching: any scope (default), file name only, or the stored text only
curl 'http://127.0.0.1:5000/api/database/records?q=invoice&scope=filename'
curl 'http://127.0.0.1:5000/api/database/records?q=ACME%20invoice&scope=content'

curl  http://127.0.0.1:5000/api/database/records/1
```

The listing answers with the applied filters next to the rows, so a client can tell
an empty result from a rejected one - `total`/`pages` describe the whole match, while
`count` is the page it returned (`page` is clamped to the real page count):

```json
{"ok": true, "query": {"q": "invoice", "scope": "filename", "limit": 10, "page": 1,
 "count": 1, "total": 1, "pages": 1},
 "records": [{"id": 12, "filename": "invoice.pdf", "preview": "ACME invoice 2026", ...}]}
```

**`run.py` connects at start-up by default** (`DATABASE_AUTO_CONNECT`; the shipped
config default is off, the development entry point turns it on). It creates the
schema before it takes traffic, so uploads are stored from the very first one instead
of only after a manual trip to `/database`. With `DATABASE_BACKEND=auto` an unreachable
MySQL server falls back to the SQLite file, so the service always starts with a working
store; with `mysql` the failure is logged and the service starts without one.
`DATABASE_AUTO_CONNECT=0` (or the older `MYSQL_AUTO_CONNECT=0`) keeps the
connect-by-hand behaviour - the start-up log then says, in as many words, that uploads
are not being saved.

## 6. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Upload form |
| `POST` | `/upload` | Multipart `file` field -> HTML result page |
| `GET` | `/result/<id>` | Re-open a stored result (kept for `RESULT_TTL_SECONDS`) |
| `GET` | `/result/<id>/download` | Extracted text as a UTF-8 `.txt` attachment |
| `GET` | `/database` | Connection form (MySQL + SQLite), live status and the stored records table (`?q=`, `?scope=`, `?limit=`, `?page=`) |
| `POST` | `/database/connect` | Connect the submitted store (`backend=mysql`/`sqlite`) - schema or file created when missing; MySQL credentials are remembered |
| `POST` | `/database/schema` | Re-run `CREATE TABLE IF NOT EXISTS` on the live store |
| `POST` | `/database/disconnect` | Close the connection (saved details are kept) |
| `POST` | `/database/forget` | Delete `instance/mysql_connection.json` |
| `GET` | `/database/records` | Records view: every stored extraction, searchable (`?q=`, `?scope=`=`all`/`filename`/`content`, `?limit=`=`all`/rows per page, `?page=`) |
| `GET` | `/database/records/<id>` | One stored extraction with its per-page text |
| `GET` | `/database/records/<id>/download` | Stored text as a `.txt` attachment |
| `GET` | `/database/records/export.xlsx` | The rows shown in the records table as an Excel workbook (`?q=`, `?scope=`, `?limit=`, `?page=`) |
| `GET` | `/database/records/<id>/export.xlsx` | One stored extraction (with its pages) as an Excel workbook |
| `POST` | `/database/records/<id>/delete` | Delete a record (its page rows cascade) |
| `POST` | `/api/ocr` | Same pipeline, JSON response |
| `GET` | `/api/health` | Engine path/version/languages, limits and the database state |
| `GET` | `/api/database` | Connection status, both backends, driver version, stored row count |
| `POST` | `/api/database/connect` | Connect with a JSON body (`backend`/`host`/... or `backend`/`path`); creates what is missing |
| `POST` | `/api/database/disconnect` | Close the connection |
| `GET` | `/api/database/records` | Newest first; `?q=` searches (file name, text or record id), `?scope=`, `?limit=` (`all`, else max 200) and `?page=`; no `?limit=` returns the whole store; answers with `total`/`pages` |
| `GET` | `/api/database/records/<id>` | One record including its pages |
| `DELETE` | `/api/database/records/<id>` | Delete one record |

Error responses are HTML for browsers and JSON for `/api/*`:

```json
{"ok": false, "error": {"code": "too_many_pages",
 "message": "This PDF has 40 pages but the limit is 25 per upload."}}
```

### JSON API examples

```bash
curl -F "file=@invoice.pdf" http://127.0.0.1:5000/api/ocr
```

```json
{
  "ok": true,
  "result_id": "8f0d1c...",
  "filename": "invoice.pdf",
  "kind": "pdf",
  "page_count": 3,
  "char_count": 115,
  "word_count": 18,
  "confidence": 95.56,
  "duration_ms": 1081,
  "engine": {"name": "tesseract", "version": "5.4.0.20240606", "languages": "eng"},
  "text": "----- Page 1 of 3 -----\n\nACME purchase order ALPHA section one\n\n...",
  "pages": [
    {"page_number": 1, "method": "ocr", "char_count": 37, "word_count": 6,
     "confidence": 95.33, "duration_ms": 246, "text": "ACME purchase order ALPHA section one"}
  ],
  "download_url": "/result/8f0d1c.../download"
}
```

`method` is `ocr` for rendered pages and `embedded` for pages that already had a
text layer (`confidence` is `null` for those, because nothing was recognised).

## 7. Configuration

Every setting is an environment variable; the defaults are production-ish. Set
them in the shell before `python run.py`, or pass overrides to
`create_app({...})` in your own entry point.

| Variable | Default | Meaning |
|---|---|---|
| `TESSERACT_CMD` | auto-detected | Full path to `tesseract.exe` / `tesseract` |
| `OCR_LANGUAGES` | `eng` | Tesseract language(s), e.g. `eng+deu` (packs must be installed) |
| `OCR_PSM` | `3` | Page segmentation mode (`6` for a single uniform block of text) |
| `OCR_OEM` | `3` | OCR engine mode (default LSTM) |
| `OCR_TIMEOUT_SECONDS` | `120` | Per-page OCR timeout |
| `OCR_DPI` | `250` | Rendering resolution for PDF pages (150 is faster, 300 sharper) |
| `OCR_MIN_TARGET_PX` | `1800` | Longest edge an image is upscaled to before OCR |
| `OCR_MAX_UPSCALE` | `3.0` | Cap for that upscaling |
| `OCR_MAX_RENDER_PIXELS` | `40000000` | Pixel budget per rendered PDF page |
| `MAX_IMAGE_PIXELS` | `50000000` | Pixel budget for an uploaded image |
| `MIN_EMBEDDED_TEXT_CHARS` | `50` | Text length above which a PDF text layer is trusted |
| `MAX_PDF_PAGES` | `25` | Requests with longer PDFs are rejected |
| `MAX_UPLOAD_MB` | `16` | Upload limit (`MAX_CONTENT_LENGTH` = this * 1 MiB) |
| `PREVIEW_MAX_PX` | `360` | Size of the inline first-page thumbnail |
| `RESULT_TTL_SECONDS` | `1800` | How long a result stays available for re-opening/download |
| `RESULT_CACHE_SIZE` | `50` | Max cached results (oldest evicted first) |
| `SECRET_KEY` | `dev-secret-change-me` | Set in any shared/production deployment |

MySQL storage (`/database` page, §5) - the `MYSQL_*` values pre-fill the form and
describe the server; the `DATABASE_*`/`SQLITE_*` values pick the store:

| Variable | Default | Meaning |
|---|---|---|
| `MYSQL_HOST` | `127.0.0.1` | MySQL server pre-filled into the connection form |
| `MYSQL_PORT` | `3306` | MySQL port |
| `MYSQL_USER` | `root` | MySQL user |
| `MYSQL_PASSWORD` | *(empty)* | MySQL password (never rendered back into the page) |
| `MYSQL_DATABASE` | `flask_ocr` | Schema created on connect when missing (`CREATE DATABASE IF NOT EXISTS`) |
| `MYSQL_TABLE` | `ocr_extractions` | Table with one row per upload |
| `MYSQL_CHARSET` | `utf8mb4` | Charset for the schema and the tables |
| `MYSQL_CONNECT_TIMEOUT` | `8` | Seconds to wait for the server when connecting |
| `MYSQL_AUTO_CONNECT` | `false` | Old name of `DATABASE_AUTO_CONNECT` (still honoured) |
| `MYSQL_AUTO_SAVE` | `true` | Old name of `DATABASE_AUTO_SAVE` (still honoured) |
| `MYSQL_REMEMBER_SETTINGS` | `true` | Default for the "remember these details" checkbox |
| `MYSQL_SETTINGS_FILE` | *instance folder* | Where the remembered connection is written |
| `MYSQL_RECORDS_LIMIT` | `200` | Default rows per page in the records table, i.e. the whole store up to the `?limit=` cap of 200 (`?limit=`/`?page=` are accepted; a lower value pages by default) |
| `DATABASE_BACKEND` | `auto` | Which store `/database` connects: `auto` (MySQL, SQLite as the start-up fallback), `mysql` or `sqlite` |
| `DATABASE_AUTO_CONNECT` | `false` | Connect at start-up using the values above (`true`/`1`). `run.py` turns it **on** unless the environment says otherwise, so a `python run.py` service saves what it extracts |
| `DATABASE_AUTO_SAVE` | `true` | Store every extraction while a store is connected |
| `SQLITE_PATH` | *instance folder*/*ocr_records.sqlite3* | File the SQLite backend uses; `:memory:` for a throw-away database |
| `SQLITE_TABLE` | `ocr_extractions` | SQLite table with one row per upload (its per-page table is `<table>_pages`) |
| `SQLITE_TIMEOUT` | `8` | Seconds an SQLite connection waits for a lock held elsewhere |

Only `Database`-level settings exist for SQLite: the store has no host, port, user
or password to configure.

Example:

```powershell
$env:OCR_LANGUAGES = "eng+deu"
$env:OCR_DPI = "300"
$env:MAX_PDF_PAGES = "50"
env\Scripts\python.exe run.py
```

```powershell
# no MySQL on this machine: use the SQLite file (nothing to install)
$env:DATABASE_BACKEND = "sqlite"
$env:SQLITE_PATH = "instance/test.sqlite3"
env\Scripts\python.exe run.py
```

## 8. Project layout

```
flask_ocr/
├─ run.py                     # dev entry point (python run.py [--port] [--debug])
├─ pyproject.toml             # pytest configuration
├─ requirements.txt           # runtime deps (requirements-dev.txt adds pytest)
├─ app/
│  ├─ __init__.py             # create_app() factory, engine + result store wiring
│  ├─ config.py               # env-driven Config and Tesseract discovery
│  ├─ exceptions.py           # domain errors -> HTTP status codes
│  ├─ error_handlers.py       # HTML for browsers, JSON for /api
│  ├─ database.py             # PyMySQL layer: settings, schema creation, queries
│  ├─ sqlite.py               # the same two tables in one file, standard library only
│  ├─ excel.py                # dependency free .xlsx writer + the records workbook
│  ├─ routes.py               # pages, /database admin, /api/ocr, /api/database
│  ├─ storage.py              # thread safe TTL cache for results
│  ├─ utils.py                # size formatting, content-negotiation helpers
│  ├─ ocr/
│  │  ├─ engine.py            # Tesseract wrapper + TSV -> text reconstruction
│  │  ├─ images.py            # decode, EXIF rotate, grey/contrast/upscale, previews
│  │  └─ documents.py         # kind detection, image path, hybrid PDF path
│  ├─ templates/              # base / index / result / database / records (+ partials)
│  └─ static/                 # style.css, app.js (no CDN - works offline)
└─ tests/                     # pytest suite, OCR tests skip without Tesseract
```

`tests/test_sqlite.py` drives a real SQLite file (so the whole storage path is
covered without a MySQL server); `tests/test_database.py` drives a recording fake of
the PyMySQL connection instead.

## 9. Tests

```powershell
env\Scripts\python.exe -m pip install -r requirements-dev.txt
env\Scripts\python.exe -m pytest -q
```

* `tests/test_validation.py` - extensions, magic bytes, empty/oversized files,
  page limit, 404/405/500 handling, JSON error contract. No Tesseract needed.
* `tests/test_ocr_image.py` - PNG upload -> text, result re-open, `.txt`
  download, JSON API, blank page.
* `tests/test_ocr_pdf.py` - 3-page scanned PDF (per-page OCR), digital PDF
  (text layer, no OCR), first-page preview.
* `tests/test_units.py` - TSV parsing/confidence, embedded-text threshold,
  image pre-processing, pixel budget, upload sniffing, result cache TTL, and the
  `.xlsx` writer (package parts, escaping, cell types, Excel's own limits).
* `tests/test_database.py` - MySQL storage with **no server required**: a small
  fake of the PyMySQL surface records every statement, so the DDL, the
  create-if-missing schema logic, the `INSERT` parameters (file name, UTC
  timestamp, content, page rows), the row mapping, credential remembering, the
  `/database` UI, the JSON API and the "database is down" fallback are all
  asserted. The fake also implements just enough of `LIKE` to prove the **records
  view**: that the search term is bound and escaped (`%`, `_`, `!`), that the
  scope restricts the columns, that a number also matches the record id, that the
  string is clamped, that the match count comes from the same clause, and that the
  pages render the right rows, summaries, page buttons and empty states - including
  a page past the end and the clamped page size. The **Excel export** is covered end
  to end as well: that the export
  query selects the whole text (with the same filters, scope and clamped limit),
  that the pages of the exported records come back in one bound query, and that the
  download is a real workbook with the right headers, sheets and "not connected"
  behaviour.
* `tests/test_sqlite.py` - the **SQLite store against a real database file** (or
  `:memory:`): the settings and DDL builders, creating the file/tables/indexes and
  recreating a dropped table, saving (timestamps, page rows, the whole transaction
  rolling back when a page insert fails), the cascading delete, the shared search
  semantics (`%`/`_`/`!` escaped, scopes, record numbers, the row cap), paging
  (`COUNT(*)` for the page buttons, `?limit=`/`?page=`, `OFFSET` only from page 2),
  the export
  columns and the one-query pages read, the status payload, the manager's backend
  choice and start-up fallback, and the whole HTTP path - connect, upload, records
  view, search, record page, `.xlsx` export and delete - without any MySQL anywhere.
  It runs on the shared `sqlite_*` fixtures described below, so every test gets
  its own throw-away file.

Test documents are generated in memory (Pillow for images and scanned PDFs, a
hand written 675-byte PDF for the text-layer case), so there are no binary
fixtures to maintain. Tests that need the engine use the `requires_tesseract`
fixture and are skipped with an explanatory message when it is missing.

### Testing against a real database file

Neither storage test file needs a server: `tests/test_database.py` drives a fake
PyMySQL, and `tests/test_sqlite.py` talks to **real SQLite files** in pytest's
`tmp_path`. Three fixtures in `tests/conftest.py` hand that store to any test, each
test getting its own throw-away file (`tmp_path/ocr_records.sqlite3`) that pytest
deletes afterwards:

| Fixture | What you get |
|---|---|
| `sqlite_path` | The `Path` of the fresh database; the file itself appears when a store connects |
| `sqlite_app` / `sqlite_client` | The app / a test client wired to `DATABASE_BACKEND=sqlite` + that `SQLITE_PATH`, **already connected** |
| `sqlite_store` | The live `SqliteDatabase`, for assertions with no HTTP round trip |

```python
import sqlite3


def test_the_invoice_is_stored(sqlite_client, sqlite_path, text_pdf_factory):
    upload = sqlite_client.post(
        "/upload",
        data={"file": (io.BytesIO(text_pdf_factory("ACME invoice 2026")), "invoice.pdf")},
        content_type="multipart/form-data",
    )
    assert "Saved to SQLite as" in upload.get_data(as_text=True)

    with sqlite3.connect(str(sqlite_path)) as connection:      # plain sqlite3
        rows = connection.execute("SELECT `filename` FROM `ocr_extractions`").fetchall()
    assert rows == [("invoice.pdf",)]
```

This is the quickest way to test anything that touches storage (saving, searching,
the records view, the Excel export, deleting) while still running **real SQL, real
transactions and real cascades** - offline, with nothing installed. Keep
`tests/test_database.py`'s `FakeConnection` for what is **MySQL-dialect** specific
(`%s` placeholders, backticked identifiers, `LEFT()`, `ESCAPE '!'`), which SQLite
spells differently and therefore cannot stand in for. `:memory:` is deliberately not
used: it is empty again after a reconnect, so a test that connects twice - or asserts
the file on disk - needs the `tmp_path` file.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `503` "Tesseract is not installed or could not be located" | Install it (step 1) or set `TESSERACT_CMD`; confirm with `GET /api/health` |
| OCR text is empty or garbled | Raise `OCR_DPI` (300+), check `OCR_LANGUAGES` matches the document, try `OCR_PSM=6` for single-block pages |
| `400` "This PDF has N pages but the limit is 25" | Raise `MAX_PDF_PAGES` or split the document |
| `400` "This PDF is password protected" | Remove the password; encrypted PDFs cannot be read |
| `413` on upload | Raise `MAX_UPLOAD_MB` (and `MAX_CONTENT_LENGTH` if you override it) |
| Long scans time out | Raise `OCR_TIMEOUT_SECONDS`, lower `OCR_DPI`, or send fewer pages |
| `Cannot find language 'deu'` | Install that `*.traineddata` into the Tesseract `tessdata` folder |
| `503` "Could not connect to MySQL ..." | Check host/port/reachability, that MySQL is running and that the user may connect from this machine (`bind-address`, firewall, `GRANT ... TO 'user'@'%'`) - or use the **SQLite** panel on `/database`, which needs no server at all |
| `1044`/`1045` access denied | Wrong user/password, or the user cannot `CREATE`. Use a user with `CREATE` or pre-create the schema (`CREATE DATABASE flask_ocr`) |
| `1142` on `CREATE TABLE`/`INSERT` | Grant `CREATE, SELECT, INSERT, DELETE` on `flask_ocr.*` to the user |
| "The MySQL driver (PyMySQL) is not installed" | `env\Scripts\python.exe -m pip install -r requirements.txt` |
| "storing it in MySQL failed" next to a result | The connection dropped mid-request; the text is still cached, press **Connect** again on `/database` (or connect the SQLite file) |
| "Could not open the SQLite file ..." | The folder is not writable, or the path is a URL instead of a file path. Use `SQLITE_PATH=instance/ocr_records.sqlite3` (or `:memory:`) |
| Timestamps look shifted | They are stored in **UTC**; convert in SQL with `CONVERT_TZ(uploaded_at, '+00:00', @@session.time_zone)` |
| The records view is empty / "Nothing stored yet" | Nothing has been written to the **connected** store yet. Check the badge in the header: with **Not connected** nothing was ever saved (uploads only live in the in-memory cache) - connect a store on `/database` (the SQLite file needs nothing installed) and upload again. `python run.py` connects the local SQLite file for you |
| "Nothing is connected yet, so no extraction is stored" | The records view has no connection and no form of its own; the link goes to `/database`, where the MySQL form and the SQLite panel are |
| Extractions are not being saved at all | `DATABASE_AUTO_SAVE=0`, the **Save the extracted data...** checkbox was unticked (unticked = this upload is not stored; a request that sends no `save_to_db` field at all does store), or no store is connected (`/api/database` reports the state) |
| The export only holds some of the records | It mirrors the table: by default every stored record (up to the 200-row page cap), newest first. Page by page beyond that, or narrow the search |

## 11. Design notes

* **Why PDFium (`pypdfium2`) instead of `pdf2image`?** No Poppler binaries to
  install, permissively licensed, and it exposes the embedded text layer *and*
  page rendering from a single dependency.
* **One engine call per page.** Words, line grouping, mean confidence and word
  count all come from a single `image_to_data(..., output_type=DICT)` run, and the
  TSV is reassembled into lines (`app.ocr.engine.rebuild_text_from_tsv`).
* **Nothing is persisted by default.** Uploads stay in RAM, are discarded after
  the request, and only the extracted text is cached in-process for
  `RESULT_TTL_SECONDS` so the result page can be re-opened and downloaded.
* **The storage sink is additive, never load-bearing.** `app/database.py` and
  `app/sqlite.py` are the only modules that know about a driver: one connection
  behind a lock (neither PyMySQL nor `sqlite3` connections are thread safe),
  identifiers whitelisted instead of escaped (a table name cannot be bound as a
  parameter), `CREATE ... IF NOT EXISTS` so connecting is idempotent, and a parent
  + pages insert in a single transaction. A failure is reported next to the result
  instead of raising, because a database problem must not cost the user the text
  they waited for.
* **SQLite is the same store, not a second implementation.** `app/sqlite.py`
  imports the column lists, the search clause builder, the identifier checks and the
  row serialisation from `app/database.py` and only supplies the dialect (`?`
  placeholders, `SUBSTR`, a `sqlite_master` probe, `PRAGMA foreign_keys = ON`), so
  the records view, the JSON API and the Excel export cannot behave differently
  depending on which store is connected - and the whole storage path is testable
  without a MySQL server. Timestamps are written as UTC text on purpose: sortable,
  and independent of the deprecated `sqlite3` datetime adapter.
* **The records view lists the store, and pages only when it must.** With no
  `?limit=` (or `?limit=all`) the page size **is** the storage layer's cap, so a store
  that is not empty shows every record it holds - one query, no follow-up clicks. A
  narrower `?limit=` (or a store bigger than the cap) is still one query per page
  (`LIMIT`/`OFFSET`, both bound; `OFFSET` is only appended from page 2) and the page
  buttons come from a `COUNT(*)` built by the *same* `search_clause()` - the total and
  the rows can never disagree about what a term matches. The count is also what makes
  a hand written `?page=` harmless: the requested page is clamped to the real page
  count before it is turned into an offset, and
  `clamp_record_page`/`clamp_record_offset` bound it again for a caller that does not.
* **The `.xlsx` writer is ZIP plus XML, not a dependency.** Excel is how the stored
  records actually leave the building, so `app/excel.py` builds the OPC package with
  `zipfile` and writes *inline strings*: no pandas and no openpyxl in
  `requirements.txt`, nothing to compile, and the workbook is assembled in memory
  like everything else. Cells are sanitised the way Excel demands (XML-illegal
  control characters dropped, Excel's 32,767 character cell cap, sheet names <= 31
  characters, numbers typed as numbers) and the header row is frozen and filterable,
  so the download behaves like a spreadsheet rather than a CSV in a costume.
* **Concurrency.** The dev server runs threaded and Tesseract is CPU-bound, so
  the number of concurrent OCR jobs is bounded by the process count. For real
  traffic install a production WSGI server
  (`pip install waitress` then `waitress-serve --call "app:create_app"`) and run
  one worker per core.


