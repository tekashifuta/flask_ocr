# Records view: every stored record on one page

The records view ("Stored records" / the same table on `/database`) now lists **the
whole store** whenever the store is not empty, instead of the newest 10 rows of it.

## What was added

### View default — `app/config.py`
* `MYSQL_RECORDS_LIMIT` (the page size) now defaults to `MAX_LIST_LIMIT` — **200**,
  imported from `app/database.py` so the cap has a single source of truth. The page
  size *is* the cap, which is what makes a store of up to 200 records list in full.
  Set it lower (`MYSQL_RECORDS_LIMIT=10`) to page by default again.

### HTTP layer — `app/routes.py`
* **`ROWS_ALL = "all"`** — the value the toolbar submits (and `?limit=all` accepts) for
  "every stored record". It maps to `MAX_LIST_LIMIT`, so a store holding more than the
  cap still pages at it instead of dragging an unbounded result set into the page.
* **`_record_limit()`** replaces the inline `clamp_record_limit()` call: a blank or
  unusable `?limit=` keeps the configured default (`_default_page_size()`, itself never
  wider than the cap), `all` means the cap, and a real number is clamped 1…200 exactly
  as before — a hand written URL can still neither empty the table nor widen one
  request past the cap.
* **`_record_query()`** writes `limit=all` instead of `limit=200` when the whole store
  is on screen, so the page buttons and the **Export .xlsx** link say what the selector
  says and keep the choice (`?limit=all`) stable across pages.
* **`_records_view()`** hands the template three new fields — `limit_choice` (which
  option is selected), `limit_choices` (the options, including any hand written value in
  use) and `showing_all` — and the summary line became "All 23 stored records." when the
  whole store (or every match) fits on the single page.
* The `Export` sheet's `Row limit` fact reads **"all stored records (max 200)"**
  (`_row_limit_label()`), and the JSON API's `query` echo keeps answering with the
  applied `limit`/`total`/`pages`.

### UI — `templates/_records.html`, `templates/records.html`, `static/css/style.css`
* **Rows per page** is now a **selector**: `All (up to 200)`, 10, 25, 50, 100 rows —
  plus whatever `?limit=` value is in use, so the control can never lie about the table
  under it.
* The page buttons appear only when there really is more than one page, and the note
  under the table explains the default; `.field-rows` was widened for the longer labels.

### Tests — `tests/test_database.py`, `tests/test_sqlite.py` (206 → **211**, all passing)
New coverage: 23 stored rows listed in full with no pagination
(`test_records_page_shows_every_stored_record_by_default`), the selector's options and
`?limit=10` still paging (`..._offers_all_and_the_usual_page_sizes`), a store bigger
than the cap paging at 200 with `limit=all` links (`..._pages_at_the_cap_when_the_store_is_bigger`),
the export taking the whole store and saying so
(`test_records_export_takes_the_whole_store_by_default`), and — against a **real**
SQLite file — 12 rows on one page plus the empty-search state
(`test_records_view_lists_every_stored_record`). The existing paging tests now ask for
`?limit=10` explicitly, which is exactly what the toolbar sends.

**Verified live** (SQLite store, 23 records): `/database/records` and `/database` both
render 23 rows with "All 23 stored records." and no page buttons; `?limit=10` renders
10 rows of 3 pages; an empty store keeps its "Nothing stored yet" row; a store of 205
records shows "Showing rows 1-200 of 205 stored records (page 1 of 2)."

**Limitation to note:** one page still reads at most `MAX_LIST_LIMIT` (200) rows, so a
much bigger store keeps its page buttons. That cap is what bounds the memory a single
request can use - raising it is `MAX_LIST_LIMIT` in `app/database.py` (the `COUNT(*)`
already tells the page how many rows are left).
