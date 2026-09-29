# Excel export: download the stored records as a spreadsheet

Everything is complete and verified. Here's the summary.

## What was added

### New module — `app/excel.py` (~570 lines)

A **dependency free `.xlsx` writer** plus the workbook the records view offers.

* **`workbook_bytes(sheets, title=, creator=, created=)`** — renders `Sheet` objects
  into an OPC package (ZIP + XML): `[Content_Types].xml`, `_rels/.rels`,
  `docProps/app.xml`, `docProps/core.xml`, `xl/workbook.xml`,
  `xl/_rels/workbook.xml.rels`, `xl/styles.xml` and one
  `xl/worksheets/sheetN.xml` per sheet. Built with `zipfile` and
  `xml.sax.saxutils.escape` only — **no pandas, no openpyxl, nothing to install**.
* **`Column(header, key=, width=, kind=, wrap=)` / `Sheet(name, columns, rows, freeze=, autofilter=)`** —
  `TEXT` / `INTEGER` / `DECIMAL` columns map to Excel number formats (`#,##0`, `0.00`)
  and to a header style, wrapped content cells, a thin bottom border on the header
  row, sized columns, a **frozen header** and an **auto filter**.
* **Excel's rules are enforced, not hoped for**: text is written as *inline strings*
  (so `123.pdf` cannot become a number), `nan`/`inf` become text instead of a silent
  empty cell, XML-illegal control characters are dropped, a cell is capped at
  **32,767** characters and sheet names are de-duplicated and clipped to 31 legal
  characters. ZIP entries use a fixed timestamp, so equal input yields equal bytes.
* **`records_workbook(records, pages=, facts=, title=)`** — the three sheets:
  * `Records` — the listing columns **plus the full extracted text**;
  * `Pages` — one row per page of every exported record (left out when empty);
  * `Export` — the facts (generated, server, schema/tables, search term, scope, row
    limit, rows exported) so the file explains itself.

### Storage layer — `app/database.py`

* **`search_clause(query, scope)`** — the `WHERE`/parameters of a search, extracted
  from `search_extractions` so the view and the export can never drift apart.
* **`_list_extractions(..., columns=, preview_chars=)`** — one listing query behind
  the plain list, the search *and* the export; `preview_chars=None` selects the whole
  `content` column instead of `LEFT(content, 140)`.
* **`export_extractions(query, limit, scope=)`** and
  **`pages_for_extractions(record_ids)`** — the latter reads the pages of every
  exported record in **one** `WHERE extraction_id IN (%s, ...)` query with bound ids
  (no query at all when there is nothing to export). Both are proxied by
  `DatabaseManager` like the other queries.

### HTTP layer — `app/routes.py`

* **`GET /database/records/export.xlsx`** — the rows the table is showing (same
  `?q=`/`?scope=`/`?limit=`, same 200 row cap), served as an attachment named after
  the search, e.g. `records_ACME-20260929-153012.xlsx`.
* **`GET /database/records/<id>/export.xlsx`** — one record with its pages, named
  `invoice_record_1-<stamp>.xlsx`.
* `_records_view()` gained `export_url`, `_export_query()`/`_export_facts()` build the
  link and the fact sheet, and both routes reuse the existing error contract (no
  connection → the usual `400`, a broken server → the usual `500`).

### UI

* **Export .xlsx** next to the search box (only when the table actually has rows),
  **Download .xlsx** on a record, and a sentence in the records view note explaining
  what the workbook holds. Both the database page and the records view show it (they
  share `templates/_records.html`).

### Tests — `tests/test_units.py` (24 → 36) and `tests/test_database.py` (81 → 96)

**Full suite 129 → 155, all passing.** The PyMySQL fake gained the export listing
(no preview parameter, so its `LIKE` engine indexes the patterns one step earlier)
and the `IN (...)` pages query, plus `stored_page()`/`content_sha256` helpers.
New coverage: the whole text vs. the preview, the filters/scope/limit reaching the
export, the clamped limit, the "no connection" error, the one-query pages read (and
the no-ids shortcut), the download's MIME type/`Content-Disposition`/sheets, the
per-record export 404, and the Export link on both pages.

*Writer unit tests* cover the package parts, well-formedness, header/text/number
cells, `nan`/`inf`, empty cells, markup escaping and control-character stripping, the
32,767 character cap, sheet-name legality/uniqueness, the frozen header + auto
filter, the sheet layout of the records workbook, and byte-for-byte reproducibility.

**Verified live:** the file was opened with a real OOXML reader (openpyxl, installed
into a temporary folder purely for the check — the venv and `requirements.txt` are
untouched) — sheet names, cell values and types, column widths, freeze panes, the
auto filter range and the document properties all round-trip. Through the running
dev server the endpoints answer `200` / `400` as designed
(`GET /database/records/export.xlsx` without a connection returns the standard
"No MySQL server is connected yet" page), and the exported workbook for a seeded
fake server contains the record with its full text, one row per page with the file
name, and the `Export` sheet with the applied filters.

**Limitations to note:** no MySQL server exists on this machine, so the export SQL is
verified against the recording fake rather than a live server (the query is the same
`SELECT`/`WHERE`/`LIMIT` shape the records view already runs, plus one bound
`IN`). The export deliberately mirrors the table: it is capped by the same
`?limit=` (max 200 rows) — a future "export everything" streaming mode is a bigger
change and was left out on purpose.
