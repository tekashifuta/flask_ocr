# "No data in the records view" - nothing was being stored

Reported after the show-everything change: the records view showed no data. The view
was right - **the store was empty, because nothing had ever been written to it.**

> **Update (one table):** the "/database has a form above it" branch described below is
> gone - `/database` no longer embeds the records table at all. See
> `notes/12_stored_extractions_section_removed.md`.

## Diagnosis (what was actually wrong)

* The only store on this machine, `instance/ocr_records.sqlite3`, held **0 rows** in
  both tables (`ocr_extractions`, `ocr_extractions_pages`) - so there was nothing to
  list.
* `DATABASE_AUTO_CONNECT` defaults to **off**, and nothing else connects on its own:
  an app started with `python run.py` runs *unconnected*. Uploads are then only kept
  in the in-memory result cache (`_should_save_to_database()` returns `False` when
  `manager.is_connected` is false), so they are dropped after `RESULT_TTL_SECONDS` and
  never reach any table.
* The records view said so, but with wording written for `/database` ("Connect to a
  MySQL server **above** to see (and store) extractions") - and on `/database/records`
  there is no form above, only a link to the storage page. Nothing pointed at the one
  action that fixes it.

## What was added

### `run.py` - connect a store at start-up (default on)
* `auto_connect_default()` returns ``True`` unless `DATABASE_AUTO_CONNECT` (or the
  older `MYSQL_AUTO_CONNECT`) is set in the environment, and `main()` passes it to
  `create_app()` - so `python run.py` connects **before it takes traffic** and every
  extraction is stored from the first one. With the default `DATABASE_BACKEND=auto`
  that is MySQL when it is reachable, otherwise the local SQLite file.
  `DATABASE_AUTO_CONNECT=0` restores connect-by-hand. (The `Config` default stays
  `false`: importing `create_app()` elsewhere - tests, a WSGI server - must not open a
  database on its own.)
* `report_store()` logs where the text is going, or - when nothing is connected - that
  **uploads are NOT saved** and how to fix it:
  `Storing extractions in SQLite (…\instance\ocr_records.sqlite3) - browse them at /database/records`.

### Records view - copy that matches the page it is on
* `_records.html` sets `connection_form_above = records_view.action == 'main.database_page'`
  and uses it for both the summary line and the empty-state row:
  * on `/database` (form above): "Connect to a MySQL server above to search the stored records."
  * on `/database/records` (no form of its own): "Nothing is connected yet, so no
    extraction is stored - [connect to a MySQL server] to search the records.", and the
    empty row links to **MySQL/SQLite storage** with "(the local SQLite file needs
    nothing installed) and upload again".
* Connected but empty keeps: "Nothing stored yet - extract a document and it will show
  up here."

### Tests — 212 → **215**, all passing
* `test_run_py_connects_a_store_at_start_up_by_default` - on when the environment is
  silent, off for `DATABASE_AUTO_CONNECT=0`/`MYSQL_AUTO_CONNECT=no`, on again for `1`/`yes`.
* `test_run_py_says_where_the_records_go` - the unconnected log line says uploads are
  not saved (and how to fix it); the connected one names SQLite and links the records view.
* `test_records_page_without_a_connection_offers_the_form` (MySQL) and
  `test_records_view_without_a_connection_asks_for_the_sqlite_store` - the records page
  never claims a form "above" and links to `/database` twice;
  `test_database_page_without_a_connection_points_at_the_form_above` keeps the other branch.

### Docs
README: the §3 start-up log sample now includes the store line (and `DATABASE_AUTO_CONNECT=0`),
§5 explains that `run.py` connects by default, the config table row says so, and the
troubleshooting table gained "the records view is empty / 'Nothing stored yet'",
"'Nothing is connected yet…'" and "extractions are not being saved at all".

**Verified live** (`python run.py`, no environment variables): start-up logged
`sqlite ready: …\instance\ocr_records.sqlite3` + `Storing extractions in SQLite (…)
- browse them at /database/records`, and `/database/records` rendered with the
**Connected** badge. Then, against a throw-away SQLite file: two `POST /upload` calls
answered "Saved to SQLite ... record #1/#2", `GET /database/records` answered
"All 2 stored records." with both file names, the export link `…/export.xlsx?scope=all&limit=all`,
no page buttons, a search for `2027` answering "1 match", and the rows were read back
from the file with `sqlite3` (id 1/2 + one page row each).

**Limitation to note:** data extracted *before* a store was connected cannot be
recovered - it only ever lived in the in-memory result cache (`RESULT_TTL_SECONDS`,
50 items) and was never written anywhere. From now on `run.py` has a store connected
from start-up, so this cannot happen again silently.
