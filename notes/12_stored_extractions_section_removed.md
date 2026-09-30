# "Stored extractions" removed - the records view is the only table now

The `/database` page rendered a **second, complete copy of the records table** in a
panel headed **Stored extractions**: same toolbar, same rows, same `.txt`/Delete
buttons, same **Export .xlsx** - because the markup was shared through
`templates/_records.html` with `templates/records.html`. Since the records view
(`/database/records`, "Stored records") became a page of its own with its own nav entry,
that copy was pure redundancy: it doubled the queries of a *connection* page and left
two different pages pretending to own the same list.

## What changed

### `app/templates/database.html` - connection only

* The whole `<section class="panel">` with the `<h2>Stored extractions</h2>` heading and
  the `{% include "_records.html" %}` is gone.
* The head of the page gained the button the removed panel used to hold -
  **Records view** -> `main.database_records_page` - so the table is still one click
  away (the lede already links it, and the status panel still counts
  **Stored records**).
* Side effect worth knowing: the page no longer runs the view's `COUNT(*)` plus the
  `LIMIT`/`OFFSET` listing query on every load, so opening the connect form (and every
  failed connect that re-renders it) is a single status lookup again.

### `app/templates/_records.html` - one page, one set of copy

* The `connection_form_above` flag (`records_view.action == 'main.database_page'`) and
  both of its branches are gone. The toolbar, the summary line and the empty-state row
  always say what the records page can actually point at: *"Nothing is connected yet,
  so no extraction is stored - [connect to a MySQL server] to search the records."* and
  *"...open MySQL storage to connect a store ... and upload again."*
* The header comment now states the include contract (`records.html` only).

### `app/routes.py` - the table belongs to the records page

* `_records_view()` lost its `endpoint` parameter; the endpoint is a local constant
  (`main.database_records_page`) feeding `view["action"]`, so the toolbar's action, the
  **Reset** link and the page buttons cannot drift to another page again.
* `_render_database_page()` no longer builds or passes `records_view` - it renders the
  connection form and the live status, nothing else - and its docstring plus
  `database_page()`'s say so.
* `database_record_delete()`'s fallback target (a request with no `next`, or an off-site
  one) is now `/database/records` - every delete button lives in the records view, and
  the record page passes its own `next` anyway.

### Tests - 224 -> 221, all passing

* `test_database_page_lists_the_stored_records` ->
  `test_database_page_leaves_the_records_to_the_records_view`: connected, with two rows
  in the fake server, the page must **not** contain `Stored extractions`, a file name or
  the `Search stored records` toolbar - but must contain the **Records view** button, the
  server label and the `<dt>Stored records</dt>` count.
* Deleted, because the behaviour they asserted no longer exists:
  `test_database_page_without_a_connection_points_at_the_form_above` (the "form above"
  copy), `test_database_page_pages_the_same_table_in_place` (page buttons returning to
  `/database`) and `test_database_page_carries_the_search_into_the_records_table`
  (`/database?q=` searching in place).
* `test_the_pages_that_list_records_offer_the_export` ->
  `test_the_records_view_offers_the_export` (the `/database?q=invoice` half is gone).
* `test_delete_ignores_an_off_site_next_target` now expects
  `Location: /database/records`.
* `test_records_page_without_a_connection_offers_the_form` (MySQL) and
  `test_records_view_without_a_connection_asks_for_the_sqlite_store` (SQLite) assert the
  empty-state sentence *"Nothing is connected yet, so extractions are not being stored"*
  instead of the removed "above to see (and store) extractions" copy.

### Docs

README §5 now says the records are browsed in the records view (the `/database` page
keeps the connection, the live status and the row count), the paging section no longer
claims `/database` reads `?q=`/`?scope=`/`?limit=`/`?page=`, the Excel-export bullet says
the export is offered by the records view, and the route table describes `GET /database`
without the records table.

## Verified

* `env\Scripts\python.exe -m pytest -q` -> **221 passed** (MySQL fake, real SQLite file,
  OCR/PDF tests included).
* `GET /database` with a connected SQLite store holding rows: no `Stored extractions`
  panel, no rows, `Records view` button present.
* `GET /database/records` unchanged: toolbar, rows, paging, **Export .xlsx**, and the
  delete buttons still return to the search they came from.

## Limitations to note

* `?q=`/`?scope=`/`?limit=`/`?page=` are now **ignored** on `/database` (they used to
  drive the embedded table). Bookmarks pointing at `/database?q=...` should move to
  `/database/records?q=...`; the page itself says where the table lives.
* The JSON API (`GET /api/database/records`) and every record/export/download/delete
  route are untouched - only the duplicated UI is gone.
