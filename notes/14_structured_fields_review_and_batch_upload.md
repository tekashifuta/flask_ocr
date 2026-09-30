# Structured fields, a review step before saving, and batch upload

Three capabilities were added to the OCR service:

1. **Structured field extraction** - a sample document (invoice or receipt) now
   yields its supplier, invoice number, document date, total amount and currency,
   not just a wall of text.
2. **Review and correction before saving** - OCR proposes, a human confirms: the
   browser flow is `POST /upload` -> review page -> `POST /review/save`, and only
   what is left in the fields is stored.
3. **Batch upload** - one form, up to `MAX_BATCH_FILES` (10) documents, one review
   page, one save.

## What was added, file by file

| File | What it does |
|---|---|
| `app/fields.py` (new) | `extract_fields()` (label driven rules + fallbacks), the normalisers (`normalize_date`, `parse_amount`, `normalize_currency`, `clean_identifier`), the `FieldValue`/`DocumentFields` model with a **confidence** per value, and `validate_field`/`validate_fields` - the single validator behind both the form and the JSON API |
| `app/review.py` (new) | `ReviewDocument` / `ReviewBatch` (one document, one upload), `documents_from()`, `submitted_result_ids()`, `submitted_field_values()` and `parse_form()` - which turns a submitted review into reviewed documents *or* into per-field messages, without ever writing anything |
| `app/ocr/documents.py` | `ExtractionResult` gained a `fields` member, filled by `extract_fields()` from the finished text - so the UI, the JSON API and storage all see the same values |
| `app/database.py`, `app/sqlite.py` | five new columns (`supplier`, `invoice_number`, `document_date`, `total_amount`, `currency`), `save_extraction(..., fields=...)`, a shared `extraction_params()` so the two dialects cannot bind a value into the wrong column, and `_add_field_columns()` - an idempotent `ALTER TABLE` upgrade for tables created before the fields existed |
| `app/routes.py` | `/upload` now takes a batch and renders the review page; new `/review/<id>` (re-open) and `/review/save` (validate + store); new `/api/ocr/batch` and `/api/review/save`; `/api/ocr` answers with `fields`, `field_confidence` and `review_url`; the records view gets a one-line field summary per row |
| `templates/review.html` (new), `_fields.html` (new) | the review form (one card per document: preview, editable fields with confidence badges and "found in", collapsed text, *Store this document*) and the read-only field list used by the result, record and review pages |
| `templates/index.html`, `result.html`, `record.html`, `_records.html` | `multiple` file input, the review copy, the field panels and the new records column |
| `static/js/app.js`, `static/css/style.css` | the drop zone lists and validates a whole batch, the review form marks a changed field as *corrected* and shows a spinner while saving; styles for the review cards, the field grid and the confidence badges |
| `app/excel.py` | the `Records` sheet carries the five fields as their own columns |
| `sql/*.sql` | the field columns in the DDL, seed values for the sample invoice, and the commented `ALTER TABLE` block an older table needs |
| `tools/make_samples.py`, `verify_samples.py` | a sixth sample - an invoice with every field labelled - and a field check per sample (`found/total` in the report) |
| `tests/test_fields.py`, `tests/test_review.py` (new) | 43 + 34 tests; the first needs no OCR and no database, the second drives the whole flow against a real SQLite file and covers the JSON endpoints and the column upgrade |

## Decisions worth writing down

* **The browser flow stores on the review step, not on upload.** That is the
  requirement, and it means `/upload` is no longer the page that reports *Saved as
  record #n* - the review page is. `tests/test_sqlite.py`, `tests/test_database.py`
  and `tests/test_ocr_image.py` were updated accordingly: they upload, assert that
  *nothing* was stored, then submit the review form (a shared `review_save` fixture
  posts the `result_id` values the review page itself rendered).
* **The JSON API keeps its one-shot behaviour.** There is no reviewer in front of
  `POST /api/ocr`, so it still stores what it extracts (opt out with
  `save_to_db=0`) and answers with the fields plus a `review_url`; `POST
  /api/review/save` is there for a script that wants to correct a value before it is
  written. `POST /api/ocr` *refuses* a multi-file request with a pointer to
  `/api/ocr/batch` rather than silently ignoring the extra files.
* **Five columns instead of a JSON blob.** The values are what a records view, a
  search and a spreadsheet want; a `fields_json` column would have been easier to
  add but useless to query. `content_sha256` stayed the *last* bound parameter so
  the existing INSERT tests (and the `sql/` scripts) still describe the row exactly.
* **The migration runs on connect.** `CREATE TABLE IF NOT EXISTS` does nothing to
  an existing table, so a store created by an earlier build would have failed every
  save with "no such column". MySQL has no `ADD COLUMN IF NOT EXISTS` (and SQLite's
  is recent), so both stores probe their own catalogue
  (`information_schema.COLUMNS`, `PRAGMA table_info`) and add only what is missing -
  in place, leaving every stored row alone. `tests/test_database.py` and
  `tests/test_review.py` both cover it, the latter against a real file written with
  the old DDL.
* **Nothing is lost when a value cannot be read.** The rejected submit re-renders
  the review page with the message next to the field, the previous (valid) value
  still in the input, and **nothing** stored - and the message quotes what was sent
  (`Got 'not a number'. Use a number such as 128.50 or 1.234,56.`), because the
  input keeps the old value rather than echoing the typo.
* **The fields are heuristics and say so.** Every value carries the confidence of
  the rule that found it (90 = a label in the document, 60 = the only candidate,
  45 = a guess), which is what the badge on the review page shows. The rules that
  have nothing to go on - the supplier header line, a bare invoice number - are only
  attempted for a document that looks like an invoice or a receipt at all, so a
  quarterly report does not get a fictitious supplier.

## Verified

* `pytest -q` -> **301 passed** (was 221: 80 new tests, 11 existing ones updated for
  the new flow).
* `tools/verify_samples.py` -> **6/6** samples, 6 records, 8 page rows, with the new
  field column showing `4/5`, `5/5`, `3/5`, `0/5`, `1/5`, `2/5` and the stored
  values printed from plain `sqlite3`.
* Live server: a two-file `/upload` renders two review cards and stores nothing;
  correcting a total (`1.234,56`) and saving stores `1234.56`; an unreadable amount
  answers `400` with `Got 'not a number'.` and stores nothing; `/database/records`
  and `/database/records/<id>` show the reviewed supplier/no./date/total, and the
  `.xlsx` export carries them as separate columns.
* `sql/sqlite_schema.sql` still applies twice cleanly, and the seeded rows now carry
  their field values (`ACME`, `10042`, `128.50`, `EUR`).

