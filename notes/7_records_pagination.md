# Records view: paged, 10 rows at a time

The records view ("Stored records" / the table on `/database`) now reads **one page of
10 rows** and draws **page buttons**, instead of listing the newest 25 and stopping.

## What was added

### Storage layer — `app/database.py` (+ `app/sqlite.py` for the SQLite store)
* **`offset=` on every listing query** (`recent_extractions`, `search_extractions`,
  `export_extractions`, and the shared `_list_extractions`). The bound parameter is
  only appended **from page 2 on** (`... LIMIT %s OFFSET %s`), so page 1 runs exactly
  the statement the view has always run - same plan, same rows, same recorded SQL.
* **`count_extractions(query, *, scope=)`** — `SELECT COUNT(*)` built by the *same*
  `search_clause()` as the listing, so the page count can never disagree with the
  rows it counts. Two queries per page in total (one count, one page).
* **`clamp_record_page()` / `clamp_record_offset()`** next to `clamp_record_limit()`,
  with `MAX_RECORD_PAGE = 10_000` as the bound for a hand written `?page=`. The
  storage layer re-clamps, so a caller that skips the route helpers cannot ask for an
  unbounded offset.
* `clamp_record_limit()` gained a `default=` argument: the view passes the configured
  **page size**, so a junk `?limit=` falls back to 10 instead of to the storage
  layer's own 25.

### HTTP layer — `app/routes.py`
* `_record_filters()` also validates **`?page=`** (1-based, clamped).
* `_records_view()` counts, clamps the page to the real page count (a `?page=99`
  lands on the last page instead of an empty table), reads exactly one page, and hands
  the template `page`, `pages`, `total`, `range_label` ("11-20"), `page_links`
  (a windowed list with `None` marking a gap), `prev_url`/`next_url`.
* `_record_query()` / `_page_url()` build the links: the current `q`/`scope`/`limit`
  are carried over, and page 1 stays implicit so short URLs stay short.
* **Export follows the page**: `?page=` is clamped the same way, the query gets the
  matching `OFFSET`, and the `Export` sheet lists the page next to the row limit.
* **JSON API**: `?page=` is honoured and the applied filters now echo `page`, `count`,
  `total` and `pages`.

### UI
* **Rows** became **Rows per page** (default `MYSQL_RECORDS_LIMIT`, now `10`).
* Summary line: "Showing rows 11-20 of 340 stored records (page 2 of 34)." - a search
  reads "340 matches for “invoice” in file name and text, rows 11-20 (page 2 of 34)",
  while a list that fits on one page keeps the short "Newest 12 of 12 stored records."
* `Previous` / numbered pages / `Next` **under the table** (`.records-pagination`,
  current page highlighted, the ends rendered as disabled spans), plus "Page 2 of 34
  · 340 records". Gaps are printed as `…`, so 400 pages are 7 buttons, not 400 links.
* The same partial serves `/database`, whose buttons stay on `/database`.

### Tests — `tests/test_database.py` (96 → 105) and `tests/test_sqlite.py` (39 → 42),
**full suite 197 → 206, all passing**
The fake PyMySQL driver grew two things so paging is observable offline: `OFFSET`
support in its listing interpreter and a `COUNT(*)` that applies the same `LIKE`
filtering as the search. New coverage: the clamp helpers (`0`, `-2`, junk, the cap),
`LIMIT`/`OFFSET` binding and the unchanged page-1 statement, the count query and its
bound pattern, ten rows a page with the right rows/URLs/summary on pages 1-3, `?page=`
past the end and junk, the search surviving the page buttons, the export of a page,
the API's `page`/`total`/`pages` echo, and the same table paging in place on
`/database`. The SQLite suite proves it against a **real** database file.

**Limitation to note:** still no MySQL server on this machine, so the `COUNT(*)`/`OFFSET`
SQL is verified against the recording fake (statement text and bound parameters) rather
than a live server - do one paged search against your own server as the final check.

> **Update (show everything):** see `notes/8_all_records_view.md` — the default page
> size is now the storage layer's cap (`MYSQL_RECORDS_LIMIT=200`), so a store that is
> not empty lists **all** of its records on one page and the page buttons only appear
> for a store bigger than 200 rows or when a page size is chosen in the toolbar (which
> is now a selector: `All (up to 200)`, 10, 25, 50, 100 rows). The `?limit=`/`?page=`
> mechanics described above are unchanged and still tested — with `?limit=10` asked for
> explicitly.

> **Update (empty view):** an empty records view turned out to mean an **empty store** —
> nothing was connected, so uploads were never saved. See
> `notes/9_records_view_was_empty.md`: `run.py` now connects a store at start-up by
> default and the records page links to the connection form instead of claiming one
> "above" it.
