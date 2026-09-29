# Records view: browse and search the stored extractions

Everything is complete and verified. Here's the summary.

## What was added

### Storage layer — `app/database.py`
* **`MySqlDatabase.search_extractions(query, limit=25, *, scope="all")`** — finds a
  record by a snippet of the **file name**, of the **stored text**, or by its **id**.
  A blank term is not an error: it falls through to `recent_extractions`, so one
  method drives the plain list and a search.
* **`_list_extractions(where, params, limit)`** — the single listing query behind
  both reads (was inline in `recent_extractions`), so the searchable and the plain
  list can never drift apart. `_LIST_COLUMNS` is now a constant.
* **Bound, escaped `LIKE` patterns** — the term becomes `%…%` with `!`, `%` and `_`
  escaped (`like_pattern`), and the SQL is
  ``WHERE (`filename` LIKE %s ESCAPE '!' OR `content` LIKE %s ESCAPE '!' OR `id` = %s)``.
  The escape character is `!`, not `\`, so the pattern cannot depend on `sql_mode`
  (`NO_BACKSLASH_ESCAPES`) or on the driver's own escaping. Numeric terms are bound
  as an `id` (bounded by `BIGINT UNSIGNED`), non-numeric ones bind `None`.
* **Scopes** `all` / `filename` / `content` (`SEARCH_SCOPES`) chosen per query, plus
  the same clamps as before (`clean_search_term` truncates to `MAX_SEARCH_CHARS`,
  `normalize_search_scope` falls back to `all`, `clamp_record_limit` caps the limit).

### HTTP layer — `app/routes.py`
* **`GET /database/records`** — the records view: a dedicated page with the search
  box, the summary line and the table (no connection form).
* **Same table on `/database`**, which now accepts the same `?q=`/`?scope=`/`?limit=`
  and links to the records view; the shared markup lives in
  `templates/_records.html` (included by `database.html` and `records.html`).
* **`GET /api/database/records?q=&scope=&limit=`** — the applied filters are echoed
  under `"query"` (`{q, scope, limit, count}`) so a client can distinguish an empty
  result from a rejected request.
* **Delete returns to the list you came from** (hidden `next` field); only same-site
  paths are honoured, and `/database/records/<id>` sends the user back to the view.
* A snippet column (the `LEFT(content, 140)` preview the queries already selected)
  shows *why* a row matched.

### UI
* Search toolbar (term, scope selector, row count), summary line ("1 match for
  “invoice” in file name only", "Newest 25 of 340 stored records"), Reset button.
* `Records` in the site nav, plus links from the database page and from a record.

### Tests — `tests/test_database.py` (62 → 81 tests, **full suite 109 → 128, all passing**)
The PyMySQL fake gained a tiny `LIKE` engine (`like_matches`, `FakeConnection._search`)
so filtering is observable without a server. New coverage: `LIKE` escaping of
`%`/`_`/`!` and the bound parameters, both scopes, the unknown-scope fallback, the
record-id shortcut (incl. the `BIGINT UNSIGNED` bound), blank/long terms, the
`/database/records` page (rows, echoes, summaries, empty state, no connection, a
server that stopped answering, a silly `?limit=`), the API filter echo, and the
delete redirects (including the rejected off-site `next`).

**Limitation to note:** no MySQL server exists on this machine, so the search SQL is
verified against the recording fake (plus the emitted statement/parameters) rather
than a live server — do one search against your own server as the final acceptance
check.

> **Update (paging):** see `notes/7_records_pagination.md` — the listing is now read
> one page at a time (`MYSQL_RECORDS_LIMIT` is the **page size**, 10 by default) with
> Previous/Next and numbered page buttons, a `?page=` parameter, and an export that
> follows the page shown. The `Newest 25 of 340` summary line quoted above is now
> `Showing rows 1-10 of 340 stored records (page 1 of 34)`. The storage layer's own
> default for a bare `recent_extractions()` call is still `25` (`DEFAULT_LIST_LIMIT`).
