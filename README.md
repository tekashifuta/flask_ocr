# Flask OCR - text extraction from images and PDFs

A small Flask service that extracts text from **JPG/PNG images** and **PDF
documents** - including scanned pages and multi-page PDFs - using **Tesseract**
as the OCR engine. It ships a browser UI (drag & drop, per-page results, copy and
download) plus a JSON API for automation.

| | |
|---|---|
| Web UI | `POST /upload` (one file or a batch) -> **review page** -> `POST /review/save` |
| Structured fields | Supplier, invoice number, date, total amount and currency, extracted from the text, **corrected by you**, then stored in their own columns |
| Batch upload | Up to `MAX_BATCH_FILES` (10 by default) files per upload, reviewed and stored together |
| JSON API | `POST /api/ocr` -> structured result, `GET /api/health` -> engine status |
| OCR engine | Tesseract 5.x through `pytesseract` |
| PDF handling | PDFium (`pypdfium2`) - renders pages *and* reads text layers, **no Poppler needed** |
| Storage | Extracted text **and the reviewed fields** in **MySQL** or a **SQLite** file - connect from `/database`, schema and tables are created for you |
| Excel export | The stored records (search included) download as `.xlsx` - no extra dependency, nothing written to disk |
| Verified on | Python 3.14.6 / Windows, Tesseract 5.4.0, Flask 3.1.3, Pillow 12.3.0, pypdfium2 5.13.0, PyMySQL 1.2.3 |
| Privacy | Documents are processed **in memory** and never written to disk |


**Where to find what:** `sql/` holds the database scripts (schema + seed data),
`samples/` the files the application was tested with, `tools/` the scripts that
generate the samples, verify them and build the submission ZIP. §12 lists every
tool and version (including the AI assistance used), §13 the assumptions,
limitations and known issues, and §14 the deliverables and how they were verified.

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
__main__: Storing reviewed extractions in SQLite (d:\Python_Projects\flask_ocr\instance\ocr_records.sqlite3) - browse them at /database/records
```

`run.py` **connects a store before it takes traffic** (the local SQLite file unless
MySQL is configured and reachable - see `DATABASE_BACKEND`), so the data you review
and save is really stored and the records view has something to show. Set
`DATABASE_AUTO_CONNECT=0` to connect by hand from `/database` instead; the log line
then warns that reviewed data is only kept in the memory of the process.

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
                               │
                               └─────────> read the structured fields out of the text
                                           (app/fields.py: supplier, invoice number,
                                            date, total amount, currency)
                                           ──> REVIEW page (you correct them)
                                           ──> POST /review/save -> the store
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
* **Structured fields** are read from the finished text with small, readable rules
  (§4.1), and every value carries the confidence of the rule that found it. They
  are **proposals**: the review page shows them as editable inputs next to the
  extracted text, and only what is left in those fields is stored (§4.2).
* **Safety valves**: `MAX_PDF_PAGES`, `MAX_UPLOAD_MB`, `MAX_BATCH_FILES`, a render
  pixel budget (`OCR_MAX_RENDER_PIXELS`) and an image pixel budget
  (`MAX_IMAGE_PIXELS`) keep a hostile or accidental upload from exhausting memory.
  Each OCR run is bounded by `OCR_TIMEOUT_SECONDS`.

Validation happens in two stages: the extension is checked first, then the real
file signature (`%PDF-` magic bytes, Pillow format sniffing). A renamed file is
therefore rejected with `400` instead of being fed to the OCR engine. A batch is
validated the same way *before* anything is OCR'd, so one bad file cannot cost a
dozen Tesseract runs.

### 4.1 Where the fields come from (`app/fields.py`)

Five values are proposed for every document, and each one records **how** it was
found - which is what the badge next to the input on the review page says:

| Field | Filled from | Confidence |
|---|---|---|
| `supplier` | a labelled line (`Supplier:`, `Vendor:`, `Sold by:`, `From:` …) | 90 |
| | else the first header line that is not another field or a bare number (`ACME invoice 2026` -> `ACME`) - only for a document that looks like an invoice or receipt | 45 |
| `invoice_number` | a labelled line (`Invoice no:`, `Inv #`, `Reference:`, `Belegnummer:` …), separators tightened (`INV - 1 - 2` -> `INV-1-2`) | 90 |
| | else a token that looks like one (`INV-2026-0042`) | 45 |
| `document_date` | a labelled line (`Date:`, `Invoice date:`, `Datum:` …) in any usual notation: `15.03.2026`, `3/15/2026`, `15 March 2026` | 90 |
| | else the first date on the page, stored as `YYYY-MM-DD` | 60 |
| `total_amount` | the line labelled `Total`, `Amount due`, `Balance due`, `Gesamtbetrag` … (never `Subtotal`, `VAT` or `Net`), as `1234.56` | 90 |
| | else the largest amount that carries a currency marker or decimals | 45 |
| `currency` | written next to that amount (`128.50 EUR`, `€ 128,50`) | 90 |
| | else the currency the document mentions most often | 45 |

What it deliberately does **not** do: invent a supplier or an invoice number for a
document that is not an invoice (a report only yields the amounts it contains), and
read a bare four digit number as an amount (`Q1 2026` is a year, not a total). The
rules are covered field by field in `tests/test_fields.py`, and the samples are
checked against their expected values by `tools/verify_samples.py` (§14).

### 4.2 Review before saving

* **Nothing is written to the store by `/upload`.** The review page shows one card
  per document - the first page preview, the extracted text (collapsed) and the
  fields as inputs - and *it* is the form that stores: the values left in those
  fields are exactly what the store receives.
* A value that cannot be read (a date like `whenever`, an amount like `not a
  number`) comes back **on the same page**, next to its field, and nothing is
  stored - the message quotes what was sent, so a typo cannot silently become
  something else, and everything else you typed is still there.
* Amounts and dates are normalised on the way in: `1.234,56` and `1,234.56` both
  become `1234.56`, `15.03.2026` becomes `2026-03-15`, `eur` becomes `EUR`.
* Unticking **Store this document** leaves that document out and stores the rest -
  a batch of invoices must not be lost because one page was unreadable.
* The result page (`GET /result/<id>`) still shows the per-page text, the
  confidence and the fields as they were read, and links back to the review step
  while the result is still in the cache (`RESULT_TTL_SECONDS`).
* The **JSON API has no reviewer**, so it keeps the one-shot behaviour: `POST
  /api/ocr` extracts *and* stores (opt out with `save_to_db=0`) and answers with the
  fields; `POST /api/ocr/batch` does the same for several files, and `POST
  /api/review/save` stores corrected values afterwards (§6).




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
CREATE TABLE IF NOT EXISTS `ocr_extractions_pages` ( ... ); -- one row per page
```

The parent table is `MYSQL_TABLE` (default `ocr_extractions`) and the page table
is that name plus `_pages` (`PAGES_TABLE_SUFFIX`), so a renamed table keeps its
matching page table. `sql/mysql_schema.sql` is the same DDL - with the seed data
- as a script you can run by hand (§14).

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
| Tables | `ocr_extractions` and `ocr_extractions_pages` - the same columns as MySQL, one row per upload / per page |
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

Once connected, the values you **reviewed** are stored and the review page says so,
e.g. *Stored in MySQL as record #12* (or *Stored in SQLite as ...*) with a link to
the record. The review form carries a **Save the reviewed data to ...** checkbox
(ticked by default); unticking it keeps that submit out of the store - the text and
the fields stay on the page. `POST /api/ocr` stores as well (it has no reviewer) -
see `DATABASE_AUTO_SAVE` and the `save_to_db` field in §7.

The form posts `save_to_db` **twice**: a hidden `0` (so an unticked box still sends
something) and then the checkbox itself (`1` when ticked). The route reads them all
(`request.form.getlist`) and *any* truthy value means "save" - reading only the first
value would always find the hidden `0` and store nothing. A request that sends no
field at all keeps the `DATABASE_AUTO_SAVE` default.

### What is stored

`ocr_extractions` - one row per reviewed document:

| Column | Meaning |
|---|---|
| `id` | Auto increment primary key |
| `filename` | Sanitised original file name |
| `uploaded_at` | Upload date and time, **UTC**, microseconds (`DATETIME(6)`) |
| `content` | The extracted text (`LONGTEXT`, multi-page results keep page markers) |
| `supplier` | `VARCHAR(255)` - the reviewed supplier (`NULL` when there is none) |
| `invoice_number` | `VARCHAR(64)` - the reviewed invoice/document number |
| `document_date` | `DATE` - the document date, normalised to `YYYY-MM-DD` |
| `total_amount` | `DECIMAL(12,2)` - the gross total, normalised to two decimals |
| `currency` | `CHAR(3)` - the ISO code next to that total |
| `kind`, `page_count`, `char_count`, `word_count`, `confidence`, `duration_ms`, `size_bytes`, `ocr_language`, `engine_version` | The statistics the result page shows |
| `content_sha256` | SHA-256 of `content` - handy for de-duplicating |
| `stored_at` | When the row was written (`TIMESTAMP`) |

The five field columns are the ones `app/fields.py` proposes and the review page
lets you correct; they are what the records table's *Supplier / no. / date / total*
cell, the record page and the `.xlsx` export show. A store created by an **earlier
version** of this project has the table without them - connecting (or *Create
schema*) adds the missing columns in place (`ALTER TABLE ... ADD COLUMN`), leaving
every stored row untouched; older rows simply keep `NULL` fields. MySQL has no
`ADD COLUMN IF NOT EXISTS`, so the app probes `information_schema.COLUMNS` and
SQLite probes `PRAGMA table_info` first (§14 shows the SQL it runs).

`ocr_extractions_pages` - one row per page (`extraction_id`, `page_number`,
`method` = `ocr`/`embedded`, `content`, counts, confidence, duration) with
`FOREIGN KEY ... ON DELETE CASCADE`, so deleting an extraction removes its pages.

Every stored row is browsed in the **records view** (`/database/records`), not on
`/database`: that page stays with the connection - form, live status and the stored row
count - and links to the table. The records view lists every record by default (id,
file name, supplier/no./date/total, upload time, pages, characters, confidence, a text
snippet) with links to re-open the text, download it as `.txt` or delete the row.
Deleting returns to the list, with the search still applied.


### Records view - browsing and searching what is stored

`/database/records` is the **records view** - the only page that lists the store, with
a search box. The search runs **in the database** (never in Python), so
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
  `/database/records?q=invoice&scope=filename&limit=50&page=3`. It is the only page
  that reads them - `/database` is the connection form and ignores `?q=`/`?limit=`.

### Excel export (.xlsx)

**Export .xlsx**, next to the search box, downloads the rows the table is showing -
the current search, scope, row limit and page are applied - as an Excel workbook:

| Sheet | Contents |
|---|---|
| `Records` | One row per stored extraction: id, file name, upload time (UTC), type, pages, characters, words, confidence, duration, size, language, engine, storage time, SHA-256 and the **full extracted text** (the table itself only lists a snippet) |
| `Pages` | One row per page of those records - the `ocr_extractions_pages` rows, so the per-page result survives the export. Left out when there are none |
| `Export` | Which filters produced the file (term, scope, row limit, page, server, schema/table, row count), so a spreadsheet that travels by e-mail explains itself |

The header row is frozen and filterable, columns are sized to their content, numbers
stay numbers (counts get a thousands separator) and text stays text - a file name
like `123.pdf` is never turned into a number.

* `GET /database/records/export.xlsx?q=&scope=&limit=&page=` - the same filters and
  page as the view, and the same 200 row cap: the export is exactly what you are
  looking at (page by page).
* `GET /database/records/<id>/export.xlsx` - one record with its pages
  (**Download .xlsx** on the record page).
* Both are offered by the records view (and by a record page); without a connection
  the URL answers the same clear `400` as the other database endpoints.
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
| `GET` | `/` | Upload form (one file or a batch, `multiple`) |
| `POST` | `/upload` | Multipart `file` field(s) -> **review page** (nothing is stored yet) |
| `POST` | `/review/save` | Validate the reviewed fields and store the ticked documents (`result_id` + `<field>_<n>` inputs, `save_to_db`); `400` with the message per field when a value cannot be read |
| `GET` | `/review/<id>` | Review one cached result again (e.g. from the result page) |
| `GET` | `/result/<id>` | Re-open a stored result (kept for `RESULT_TTL_SECONDS`) - text, per-page confidence and the fields as read |
| `GET` | `/result/<id>/download` | Extracted text as a UTF-8 `.txt` attachment |
| `GET` | `/database` | Connection form (MySQL + SQLite), live status and the stored row count - the rows are listed in the records view, which the page links to |
| `POST` | `/database/connect` | Connect the submitted store (`backend=mysql`/`sqlite`) - schema or file created when missing, missing field columns added; MySQL credentials are remembered |
| `POST` | `/database/schema` | Re-run `CREATE TABLE IF NOT EXISTS` on the live store (and add missing field columns) |
| `POST` | `/database/disconnect` | Close the connection (saved details are kept) |
| `POST` | `/database/forget` | Delete `instance/mysql_connection.json` |
| `GET` | `/database/records` | Records view: every stored extraction, searchable (`?q=`, `?scope=`=`all`/`filename`/`content`, `?limit=`=`all`/rows per page, `?page=`) |
| `GET` | `/database/records/<id>` | One stored extraction with its per-page text and its stored fields |
| `GET` | `/database/records/<id>/download` | Stored text as a `.txt` attachment |
| `GET` | `/database/records/export.xlsx` | The rows shown in the records table as an Excel workbook (`?q=`, `?scope=`, `?limit=`, `?page=`) |
| `GET` | `/database/records/<id>/export.xlsx` | One stored extraction (with its pages) as an Excel workbook |
| `POST` | `/database/records/<id>/delete` | Delete a record (its page rows cascade) |
| `POST` | `/api/ocr` | Same pipeline, JSON response - extracts **and stores** (opt out with `save_to_db=0`); one file per call |
| `POST` | `/api/ocr/batch` | The same for several files: `{ok, count, saved, results: [...]}`, one entry per document |
| `POST` | `/api/review/save` | Store **corrected** fields for cached results (`documents` list or a single flat object) |
| `GET` | `/api/health` | Engine path/version/languages, limits and the database state |
| `GET` | `/api/database` | Connection status, both backends, driver version, stored row count |
| `POST` | `/api/database/connect` | Connect with a JSON body (`backend`/`host`/... or `backend`/`path`); creates what is missing |
| `POST` | `/api/database/disconnect` | Close the connection |
| `GET` | `/api/database/records` | Newest first; `?q=` searches (file name, text or record id), `?scope=`, `?limit=` (`all`, else max 200) and `?page=`; no `?limit=` returns the whole store; answers with `total`/`pages` |
| `GET` | `/api/database/records/<id>` | One record including its pages and its structured fields |
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
  "fields": {"supplier": "ACME", "invoice_number": "10042", "document_date": null,
             "total_amount": "128.50", "currency": "EUR"},
  "field_confidence": {"supplier": 45.0, "invoice_number": 90.0, "document_date": null,
                       "total_amount": 90.0, "currency": 90.0},
  "pages": [
    {"page_number": 1, "method": "ocr", "char_count": 37, "word_count": 6,
     "confidence": 95.33, "duration_ms": 246, "text": "ACME purchase order ALPHA section one"}
  ],
  "download_url": "/result/8f0d1c.../download",
  "review_url": "/review/8f0d1c...",
  "database": {"connected": true, "saved": true, "record_id": 12, "error": null}
}
```

`method` is `ocr` for rendered pages and `embedded` for pages that already had a
text layer (`confidence` is `null` for those, because nothing was recognised).
`fields` are the values the parser read, `field_confidence` says how (the same
`90`/`60`/`45` as §4.1), and `database.saved` is `true` because the API stores what
it extracts - it has no reviewer in front of it.

A batch, one entry per document:

```bash
curl -F "file=@scan_invoice.png" -F "file=@scan_receipt.jpg" \
     http://127.0.0.1:5000/api/ocr/batch
```

```json
{"ok": true, "count": 2, "saved": 2,
 "results": [{"result_id": "8f0d1c...", "filename": "scan_invoice.png", "fields": {...}},
             {"result_id": "b41c07...", "filename": "scan_receipt.jpg", "fields": {...}}]}
```

Correcting a value before it is stored - the machine equivalent of the review page:

```bash
curl -X POST http://127.0.0.1:5000/api/review/save \
     -H 'Content-Type: application/json' \
     -d '{"documents": [{"result_id": "8f0d1c...",
                         "fields": {"supplier": "Acme GmbH", "total_amount": "1.234,56"}}]}'
```

```json
{"ok": true, "connected": true, "count": 1, "saved": 1,
 "documents": [{"result_id": "8f0d1c...", "filename": "invoice.pdf",
                "fields": {"supplier": "Acme GmbH", "invoice_number": "10042",
                           "document_date": null, "total_amount": "1234.56", "currency": "EUR"},
                "corrected": ["supplier", "total_amount"], "saved": true, "record_id": 12}]}
```

Only the fields present in the request are changed (the rest keeps what the parser
read), values are normalised the same way the form normalises them, and a value
that cannot be read answers `400` with `{"fields": {"<result_id>": {"<field>":
"message"}}}` - storing nothing, so a correction can never be silently dropped.


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
| `MAX_BATCH_FILES` | `10` | Files one upload (one `/upload` batch) may contain |
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
│  ├─ fields.py               # structured fields: rules, confidence, validation
│  ├─ review.py               # the review model + the review form's parsing
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
│  ├─ templates/              # base / index / review / result / database / records
│  └─ static/                 # style.css, app.js (no CDN - works offline)

├─ sql/                       # standalone schema + seed data (MySQL and SQLite)
├─ samples/                   # the files the app was tested with (+ samples/README.md)
├─ tools/
│  ├─ make_samples.py         # regenerates samples/ (no downloads, no binaries in git)
│  ├─ verify_samples.py       # uploads every sample through the app and checks the result
│  └─ package_submission.py   # builds the submission ZIP (dist/flask_ocr_submission.zip)
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
* `tests/test_fields.py` - the structured field rules: each sample text (including
  the ones the shipped files produce), the date and amount normalisers, the
  per-field confidence, and the validation a reviewer's typo runs into. No
  Tesseract, no database.
* `tests/test_review.py` - the review model and form parsing without OCR, plus the
  whole flow against a real SQLite file: `POST /upload` renders the review page and
  stores **nothing**, `/review/save` stores the corrected values, a bad value comes
  back with its message, a document can be dropped from a batch, the batch limit is
  enforced, `/review/<id>` re-opens a cached result, and `POST /api/ocr/batch` /
  `POST /api/review/save` behave the way the JSON contract in §6 says. Also the
  migration: a table created before the field columns existed is upgraded in place.
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
| Extractions are not being saved at all | `DATABASE_AUTO_SAVE=0`, the review page's **Save the reviewed data...** checkbox was unticked (unticked = this submit is not stored; a request that sends no `save_to_db` field at all does store), or no store is connected (`/api/database` reports the state). Remember that `/upload` never stores on its own - the review page's **Save reviewed data** button does |
| The review page says a document's result expired | The review card's text and fields are re-read from the in-memory cache when you submit (`result_id`), and `RESULT_TTL_SECONDS` had passed. Upload the document again - the text itself was never lost, it simply cannot be stored from that page any more |
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

## 12. Tools, versions and AI assistance

### Tools, frameworks and libraries

Everything below was verified together on **Python 3.14.6 / Windows 11
(10.0.26100)**. The application itself runs on Python 3.10+.

| Layer | Component | Version | Notes |
|---|---|---|---|
| Language | **Python** | 3.14.6 (3.10+) | `env\Scripts\python.exe` in this checkout |
| **OCR engine** | **Tesseract OCR** | 5.4.0.20240606 (Leptonica 1.84.1) | native binary, not a pip package - `winget install -e --id UB-Mannheim.TesseractOCR`; installed languages: `eng`, `osd` |
| OCR binding | pytesseract | 0.3.13 | one `image_to_data` call per page (TSV -> text + confidence) |
| PDF engine | PDFium via pypdfium2 | pypdfium2 5.13.0 (PDFium 153.0.7999.0) | renders pages *and* reads embedded text - no Poppler |
| Image handling | Pillow | 12.3.0 | decode/verify, EXIF rotate, preprocessing, first-page preview, sample generation |
| Web framework | Flask | 3.1.3 | application factory + single blueprint |
| WSGI toolkit | Werkzeug | 3.1.9 | multipart parsing, `MAX_CONTENT_LENGTH` |
| Templating | Jinja2 (+ MarkupSafe) | 3.1.6 (+ 3.0.3) | server rendered pages |
| Remaining Flask deps | itsdangerous 2.2.0, blinker 1.9.0, click 8.5.0 | | installed with Flask |
| **Database** (optional server) | **MySQL 8.0** + PyMySQL 1.2.3 | PyMySQL 1.2.3 | pure-Python driver (no compiler needed). **No MySQL server is installed on the development machine** - see §13 |
| **Database** (default store) | **SQLite** | 3.50.4, via the standard library `sqlite3` of Python 3.14.6 | one file, no server, no credentials, nothing to install |
| Excel export | *standard library only* | `zipfile` + `xml` | no pandas, no openpyxl (see §5) |
| Front end | *none* | hand written CSS + vanilla JavaScript | no CDN, no build step, works offline |
| Tests | pytest | 9.1.1 | 301 tests |
| Packaging | *standard library only* | `zipfile` via `tools/package_submission.py` | builds the submission archive |
| Development machine | Windows 11 (10.0.26100), VS Code | | `winget` used for Tesseract, `py -3.14 -m venv` for the environment |

The complete pinned list is `requirements.txt` (runtime) and
`requirements-dev.txt` (adds pytest).

### AI tools used

The brief allows AI assistance as long as it is disclosed and understood. The
following was used while building this project:

| Tool | How it was used |
|---|---|
| **Cline** (AI coding agent in VS Code) | Drafted and refactored implementation code (`app/ocr/*`, `app/database.py`, `app/sqlite.py`, `app/excel.py`, `app/routes.py`, templates), wrote the test suite and the documentation, generated `sql/*.sql`, `samples/` and the scripts in `tools/`, and diagnosed the bugs written up in `notes/` |

**Every AI-assisted change was reviewed by reading it and verifying it by running
it** - `pytest -q` (301 tests), `tools/verify_samples.py` (the six sample files
through the real HTTP stack and a real SQLite store), the schema/seed comparison
for `sql/sqlite_schema.sql`, and a live server smoke test (§14). Nothing is in the
repository that was not executed at least once. No AI tool has access to any
credentials, and no document content was sent anywhere: the OCR runs locally
against the local Tesseract binary.

If any other assistant was used during the submission (for example ChatGPT, GitHub
Copilot or Cursor for a specific file), add it to this table - the requirement is
that the list is complete.

## 13. Assumptions, limitations and known issues

### Assumptions

1. **Tesseract is installed on the host** (it is a native binary, §1). Only `eng`
   is installed by the Windows package used here; any other language needs its
   `*.traineddata` in `tessdata/` and must be listed in `OCR_LANGUAGES`
   (`eng+deu`). If the binary is missing the app still starts and reports the
   problem through `/api/health` and the upload page instead of crashing.
2. **Local, single-user tool.** There is no authentication, no user accounts and no
   CSRF token, and the `/database` page accepts server credentials. Run it on
   127.0.0.1 (the default) or behind a reverse proxy that authenticates; do not
   expose it to a network as-is.
3. **Uploads are what they claim to be** - one of `.jpg`, `.jpeg`, `.png`, `.pdf`,
   at most 16 MB (`MAX_UPLOAD_MB`) and at most 25 PDF pages (`MAX_PDF_PAGES`).
   Content is verified against the extension (magic bytes), but there is no
   antivirus scanning.
4. **Timestamps are UTC** everywhere (upload time and `stored_at`); the UI and the
   API label them as UTC. `uploaded_at` is a naive UTC value (`DATETIME(6)` in
   MySQL, `YYYY-MM-DD HH:MM:SS.ffffff` text in SQLite).
5. **A page whose embedded text layer holds at least `MIN_EMBEDDED_TEXT_CHARS`
   (50) characters is trusted as-is** - no OCR runs on it, so a digital PDF with
   broken embedded text is reproduced verbatim rather than re-recognised.
6. **One process.** The result cache (§7, `RESULT_TTL_SECONDS`,
   `RESULT_CACHE_SIZE`) lives in the memory of a single process; a multi-worker
   deployment stores records in the database but serves `/result/<id>` only from
   the worker that created it.
7. **Structured fields are proposals, not facts.** `app/fields.py` reads them from
   the text with documented rules (§4.1) and marks how confident each rule was; the
   review page exists so a human confirms them. A document whose layout the rules do
   not recognise simply leaves fields empty - the text is still extracted and
   stored.
8. **The browser flow stores on `/review/save`, never on `/upload`.** The JSON API
   keeps extracting *and* storing in one call (§4.2) because there is no reviewer in
   front of it.
9. The commands are run from the project root; PowerShell is used for the Windows
   examples, with `bash` alternatives where a shell is involved.


### Limitations and known issues

| # | Limitation / issue | Detail, and what to do about it |
|---|---|---|
| 1 | **The MySQL path was never run against a live server** | The development machine has no MySQL server (and no Docker), so MySQL is covered by `tests/test_database.py` (a recording fake of the PyMySQL surface) and by checking that `sql/mysql_schema.sql` contains exactly the DDL `app/database.py` builds. The DDL is plain InnoDB/utf8mb4 using only `IF NOT EXISTS`. **Do one connect against your server as the final acceptance check** (`/database` -> *Connect & create schema*, or `POST /api/database/connect`). SQLite, by contrast, was executed for real end to end (schema comparison, seed data, uploads, search, records view, export, delete). |
| 2 | **Schema changes are limited to adding the field columns** | `CREATE TABLE IF NOT EXISTS` still leaves an existing table alone, and the **only** automatic upgrade is the one this submission needed: connecting (or *Create schema*) probes the columns and adds the five structured field columns with `ALTER TABLE ... ADD COLUMN` (§5), so a database from an earlier build keeps working. Any *further* column would need its own explicit `ALTER TABLE` - there is still no general migration framework, no version table and no down-migration. |
| 3 | **SQLite `LIKE` is case-insensitive for ASCII only** | The records search is case-insensitive in ASCII for both stores; with the SQLite store, case-insensitive matching does **not** happen for accented or non-Latin text (MySQL's utf8mb4 collation is not ASCII limited), so the same query can behave differently on the two stores. |
| 4 | **Search is `LIKE '%term%'`, not full text** | The term is matched against `filename`/`content` (plus the record id when it is numeric). It is bounded, escaped and always bound as a parameter, but every row's `content` is inspected - there is no full-text index and no relevance ranking. |
| 5 | **Results expire** | `/result/<id>` and its `.txt` download only work while the result sits in the in-memory cache (30 minutes, 50 entries by default). The stored record survives, and `/database/records/<id>` is the permanent view. |
| 6 | **Throughput** | Tesseract is CPU-bound and handles one page at a time, and the Flask development server is not a production server. Each page is bounded by `OCR_TIMEOUT_SECONDS` (120 s), so a very heavy page can still fail that one page. For traffic, run a real WSGI server (waitress) with one worker per core. |
| 7 | **Encrypted PDFs are rejected** (`400`, "password protected") | Nothing is decrypted - remove the password first. A PDF whose pages are images costs one OCR run per page. |
| 8 | **The `.xlsx` export is memory bound and page bound** | It mirrors what the table shows: at most the 200-row cap (`MAX_LIST_LIMIT`) per download, built entirely in memory. Cells are inline strings; spreadsheet styling is limited to number formats, a frozen header row and an autofilter. |
| 9 | **The remembered MySQL password is stored in clear text** | `instance/mysql_connection.json` (git-ignored, `chmod 600` on POSIX - Windows ACLs are not tightened) holds whatever you ticked "remember these details" for. Treat it as a secret, or untick the box / use *Forget saved details*. |
| 10 | **Duplicates are not detected automatically** | `content_sha256` is stored so duplicates can be found (`SELECT ... WHERE content_sha256 = SHA2(<text>, 256)`), but an upload is never skipped or merged on insert. |
| 11 | **The documentation named the page table `ocr_extraction_pages`** | The real, derived name is `ocr_extractions_pages` (`<MYSQL_TABLE>` + `_pages`). The code was always right; the README, one note and two docstrings said otherwise and were corrected for this submission - both SQL scripts use the derived name. |
| 12 | **The sample images depend on an OS font** | `tools/make_samples.py` renders them with Arial (Windows) or DejaVu (Linux), so file digests and the last digit of the confidence values differ between machines. The recognised text does not. |
| 13 | **Without Tesseract the OCR tests are skipped, not failed** | `pytest -q` then reports the subset that needs no engine (validation, storage, units). `tools/verify_samples.py` exits `2` with the install hint instead of pretending to have checked the images. |
| 14 | **The first-page preview is a PNG data URI** | Only page 1 of a PDF is rendered for the thumbnail (`PREVIEW_MAX_PX`) and it is embedded directly in the HTML page, which makes the result page a little larger. |
| 15 | **The field rules are heuristics** | They are label driven, so an invoice with unusual wording (`Rechnungsnummer` without a colon, a total written only as `Summe`) leaves fields empty rather than guessing wrongly; the review page is where that is fixed by hand. `dd/mm/yyyy` and `mm/dd/yyyy` are ambiguous, so day-first wins unless the first number cannot be a day - `03/04/2026` is read as 3 April 2026. A bare `1.234` is read as 1234 (thousands), and only a *labelled* total is trusted at 90 % - everything else is marked as a guess. |
| 16 | **A batch is validated before it is OCR'd** | If one file in the batch is unsupported or too large, the whole submit is refused (nothing is extracted, nothing is stored) so a typo in the file list cannot leave half a batch behind. Remove or rename the file and upload again. |
| 17 | **The review page lives in the in-memory result cache** | The card's fields and text are re-read from the cache when the form is submitted (`result_id`), so if a result expires between upload and save (`RESULT_TTL_SECONDS`, 30 minutes) that document cannot be stored any more - the page says so, and re-uploading it is the fix. |


## 14. Submission package: SQL scripts, sample files and the ZIP

### The database scripts (`sql/`)

The application creates its schema by itself when you connect a store (§5). These
two scripts are the same DDL as a file, plus seed data, for reviewers who want to
pre-create the objects or inspect them in SQL. Both are **idempotent** - running
them twice changes nothing.

| Script | Target | Contents |
|---|---|---|
| `sql/mysql_schema.sql` | MySQL 8.0 | `CREATE DATABASE IF NOT EXISTS flask_ocr` (utf8mb4), both tables with the five structured field columns, the three indexes, the unique key and the cascading foreign key, **2 seed extractions + 4 page rows** (ids 9001+, so they cannot collide with real uploads), the seed field values, a commented block with the five `ALTER TABLE ... ADD COLUMN` statements an older table needs, a `SHA2()` step that fills `content_sha256` the way the app does, verification queries and the grants a user needs |
| `sql/sqlite_schema.sql` | SQLite (the default store) | the same two tables (field columns included) and three indexes, `PRAGMA foreign_keys = ON`, the same seed rows *with* their field values and the real SHA-256 values (stock SQLite has no `SHA2()`), plus the same commented `ALTER TABLE` block |


```powershell
# MySQL (creates the schema, the tables and the seed rows)
mysql -u root -p < sql/mysql_schema.sql

# SQLite - either the CLI ...
sqlite3 instance/ocr_records.sqlite3 < sql/sqlite_schema.sql
# ... or Python, which is what this project uses (sqlite3 is part of CPython)
env\Scripts\python.exe -c "import sqlite3; sqlite3.connect('instance/ocr_records.sqlite3').executescript(open('sql/sqlite_schema.sql', encoding='utf-8').read())"
```

The seed rows are exactly what the application extracted from two of the sample
files, so the records view, the search and the `.xlsx` export can be tried without
uploading anything first. Remove them again with
`DELETE FROM ocr_extractions WHERE id IN (9001, 9002);` (the page rows cascade).

### The sample files (`samples/`)

| File | Size | Kind | Pages | Extracted as |
|---|---|---|---|---|
| `images/scan_invoice.png` | 22.8 KB | image | 1 | OCR - 53 chars, 95.11 %, 4 of 5 fields |
| `images/scan_invoice_fields.png` | 54.5 KB | image | 1 | OCR - 156 chars, 95.3 %, **all five fields labelled** |
| `images/scan_receipt.jpg` | 27.1 KB | image | 1 | OCR (JPEG path) - 50 chars, 96.00 %, 3 of 5 fields |
| `images/blank_page.png` | 2.3 KB | image | 1 | OCR - 0 chars, the "no text found" case |
| `pdf/scanned_invoice_3_pages.pdf` | 75.1 KB | pdf | 3 | OCR on **every** page - 178 chars, 95.47 % |
| `pdf/digital_report_text_layer.pdf` | 0.7 KB | pdf | 1 | **embedded** text layer, Tesseract never called - 93 chars |

`samples/README.md` documents each file in detail, the expected text, the fields it
must yield, how the files are regenerated and how they are verified. They are
synthetic (all text is fictitious), and they are produced by
`tools/make_samples.py`, so the pixel bytes

never have to be committed by hand.

### Regenerating and verifying them

```powershell
env\Scripts\python.exe tools\make_samples.py       # (re)write samples/ and print a manifest
env\Scripts\python.exe tools\verify_samples.py     # upload every sample and check the result
env\Scripts\python.exe -m pytest -q                # 301 tests
```

`tools/verify_samples.py` builds the real application (pointed at a throw-away
SQLite file), uploads each sample through the HTTP stack and asserts the kind, the
page count, the extraction method per page, the expected text, the structured fields
each document must yield and that the upload was stored - verified both through
`GET /api/database/records` and with plain `sqlite3` against the file. Its output on
this machine:

```
file                                      HTTP kind   pages extract    chars   conf fields  record  verdict
-----------------------------------------------------------------------------------------------------------
images/scan_invoice.png                    200 image      1 ocr           53   95.1    4/5       1  ok
images/scan_invoice_fields.png             200 image      1 ocr          156   95.3    5/5       2  ok
images/scan_receipt.jpg                    200 image      1 ocr           50   96.0    3/5       3  ok
images/blank_page.png                      200 image      1 ocr            0      -    0/5       4  ok
pdf/scanned_invoice_3_pages.pdf            200 pdf        3 ocr          178   95.5    1/5       5  ok
pdf/digital_report_text_layer.pdf          200 pdf        1 embedded      93      -    2/5       6  ok

stored records        : 6 (API) / 6 (SQLite)
stored pages          : 8 (SQLite), expected 8
  #1   scan_invoice.png                   image  pages=1 chars=53 fields=(ACME, 10042, 128.5, EUR) sha256=6bda0ca88e80
  #2   scan_invoice_fields.png            image  pages=1 chars=156 fields=(Northwind Trading GmbH, INV-2026-0042, 140.42, EUR) sha256=f64350f1e20c
  #3   scan_receipt.jpg                   image  pages=1 chars=50 fields=(Corner Coffee, -, 3.5, EUR) sha256=283e860214a0
  #4   blank_page.png                     image  pages=1 chars=0 fields=(-, -, -, -) sha256=e3b0c44298fc
  #5   scanned_invoice_3_pages.pdf        pdf    pages=3 chars=178 fields=(ACME, -, -, -) sha256=dfdae19e1e5f
  #6   digital_report_text_layer.pdf      pdf    pages=1 chars=93 fields=(-, -, 1240000.0, EUR) sha256=7c715819b947

All 6 sample files passed every check.
```


### Building the ZIP

```powershell
env\Scripts\python.exe tools\package_submission.py
```

writes **`dist/flask_ocr_submission.zip`** - one top-level folder `flask_ocr/`
containing the application source, the tests, `sql/`, `samples/`, `README.md` and
the development notes. `env/`, `.git/`, `instance/` (runtime state, SQLite files,
remembered credentials), `dist/` and every `__pycache__` stay out. The script
re-opens the finished archive and fails if one of the required deliverables
(README, `run.py`, `requirements.txt`, the app, the tests, both SQL scripts, the
six sample files) is missing or the archive is corrupt.


### What was verified before packaging

| Check | Result |
|---|---|
| `pytest -q` (whole suite) | **301 passed** in ~10 s |
| `tools/verify_samples.py` | **6/6** sample files, 6 records and 8 page rows in a real SQLite file, including the structured fields read back with plain SQL |
| The review flow (live server) | `POST /upload` with two files -> review page (2 cards, fields pre-filled, **no** record written); correcting a total and saving -> *Stored in SQLite as record #1, #2*; an unreadable amount -> `400` with the message next to the field and nothing stored; `/database/records` shows the reviewed supplier/no./date/total |
| `sql/sqlite_schema.sql` | applied **twice** (idempotent), then compared with the database the application creates: identical `sqlite_master` DDL and identical `PRAGMA table_info` columns; the seed digests re-computed with `hashlib` match; the app lists, searches (`q=ALPHA`), opens and exports the seeded records |
| `sql/mysql_schema.sql` | contains **verbatim** the DDL `create_database_sql` / `create_table_sql` / `create_pages_table_sql` produce for the default names (field columns included). Not executed: no MySQL server here (limitation 1) |
| Schema upgrade | a SQLite file whose `ocr_extractions` table predates the field columns: connecting adds the five columns (`PRAGMA table_info` before/after), the existing rows stay, and saving into the upgraded table works |
| Live server (`run.py --port 5055`, SQLite store) | `GET /` 200 (drop zone), `GET /api/health` 200 (`available: true`, `5.4.0.20240606`, `eng`+`osd`), `POST /api/ocr` with `scan_invoice.png` -> 200, 53 chars, 95.11 %, fields `{ACME, 10042, null, 128.50, EUR}`, *saved as record #1*, `POST /api/ocr/batch` with two files -> 200 and two records, `POST /api/review/save` -> corrected values stored, records view renders them, `GET /database/records/export.xlsx` 200 with a real workbook, an unsupported `.txt` upload -> **400** |
| `python -c "import app"` | no import-time side effects: Tesseract is located lazily and a missing engine is reported, not raised (§1) |









