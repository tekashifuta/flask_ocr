# "Stored records: 0" - the save checkbox was never read

Reported after choosing the SQLite store: nothing could be stored - the panel said
**Stored records: 0** however many documents were uploaded.

## Diagnosis (what was actually wrong)

* The store itself was healthy. `instance/ocr_records.sqlite3` existed with both
  tables, and **`sqlite_sequence` was empty as well** - since an `AUTOINCREMENT` table
  writes a row there on its first insert, that proved no insert had *ever* been
  committed. Nothing had been written to some other file, and no dialect problem was
  involved.
* The running app was connected: `/api/health` reported `provider: sqlite`,
  `connected: true`, and a raw `POST /upload` (multipart, **no** `save_to_db` field)
  was stored, counted and visible in the file. So the write path, the DDL and the
  count were all fine.
* Replaying the payload the **browser** sends found it. `app/templates/index.html`
  renders the field pair

  ```html
  <input type="hidden"   name="save_to_db" value="0">
  <input type="checkbox" name="save_to_db" value="1" checked>
  ```

  and `_should_save_to_database()` read it with `request.form.get("save_to_db")`.
  Werkzeug's `MultiDict.get()` returns the **first** value - the hidden `"0"` - so
  `_truthy("0")` was `False` and `_persist()` returned `(None, None)`: no save, no
  warning, no error. Because the box is ticked by default, **every upload submitted
  from the form silently skipped storage**, which is exactly why the store stayed at
  zero rows.
* The **connect form has the same trap** in `database.html` (hidden
  `remember=0` + ticked `remember=1`, read by `_truthy(request.form.get("remember"))`),
  which silently stopped "Remember these details" from ever writing the file the
  README promises (`instance/mysql_connection.json`). It went unnoticed because the
  tests posted a single `remember` value - the browser posts two.
* Measured against the running app before the fix:

  | Payload | Stored? |
  |---|---|
  | Browser, box ticked (`save_to_db=0` then `=1`) | **no** (the default path - the bug) |
  | Browser, box unticked (`save_to_db=0` only) | no (correct) |
  | Single `save_to_db=1` (programmatic) | yes |
  | No `save_to_db` field (JSON API) | yes |

## What was added

### `app/routes.py` - one helper, both forms

New `_checkbox_field(name, default)`: it reads `request.form.getlist(name)` and lets
*any* truthy value win, while a request with **no field at all** (the JSON API) keeps
*default*. `_should_save_to_database()` uses it with the `DATABASE_AUTO_SAVE` default,
and a second helper `_remember_requested(payload)` covers both shapes the connect
endpoints accept: a mapping (JSON, where `remember: false` is a real boolean) or a
`MultiDict` (a form, with the hidden+checkbox pair). `/database/connect` and
`/api/database/connect` therefore agree with the box the user actually ticked, and
`MYSQL_REMEMBER_SETTINGS` is only the default for a request that says nothing - which
is what the README always claimed. Both docstrings spell the contract out so it is not
"simplified" back to `.get()`.

### `app/templates/index.html` - say why the pair is there

A Jinja comment above the two inputs explains the hidden/checkbox contract and warns
that reading only the first value always finds the hidden `0`.

### Tests - 217 → **224**, all passing

* `test_upload_from_the_form_stores_the_extraction` - posts exactly what the browser
  posts (`"save_to_db": ["0", "1"]`) against a real SQLite file and asserts both the
  *Saved to SQLite as ...* notice **and** the row read back out of the file. This is
  the regression test for the bug.
* `test_unticking_the_save_checkbox_keeps_the_upload_out_of_the_store` - the hidden
  `0` on its own: the text is extracted, nothing is stored.
* `test_the_upload_form_offers_a_ticked_save_checkbox_and_its_hidden_twin` - keeps the
  template's two fields in step with what those tests post.
* `test_connect_form_remembers_the_details_when_the_box_is_ticked` - both `remember`
  values posted (what the browser sends) and the settings file **is** written; and
  `test_connect_form_does_not_remember_an_unticked_box` for the hidden `0` alone.
* `test_api_connect_remembers_only_when_the_payload_asks_for_it` - a JSON body with
  `remember: true` writes the file and `remember: false` does not (the documented
  behaviour that had no test); `test_api_connect_reads_the_hidden_checkbox_pair_too`
  covers the same endpoint posted as a form, where the ticked box must win.

### Docs

README §5 states the contract (unticked = not stored; the form posts the field twice;
no field = `DATABASE_AUTO_SAVE`), and the troubleshooting row for "extractions are not
being saved at all" now says an unticked box is honoured while a request without the
field still stores.

## Verified live

Restarted `python run.py` with the fix and replayed the whole browser journey over
HTTP: the connect panel opened the SQLite file, an upload posting `save_to_db=0`
**and** `save_to_db=1` answered *Saved to SQLite ...*, `/api/database/records` returned
both rows, the upload page showed **Stored records 2**, and the file held 2 parent + 2
page rows - where the same payload had stored nothing before the fix. The unticked,
programmatic and no-field requests behaved exactly as the table above says. Everything
written while diagnosing was deleted again (with `PRAGMA foreign_keys = ON`, so the
page rows went with the parents), leaving the store at 0 rows.

## Limitations to note

* An app started **before** this fix keeps the old route until it is restarted - the
  running process is what matters, not the file on disk.
* Extractions that were skipped are gone: they only ever lived in the in-memory result
  cache (`RESULT_TTL_SECONDS`, 50 items), exactly as in
  `notes/9_records_view_was_empty.md`. Upload them again after restarting.
* The hidden field stays on purpose: without it an unticked box would submit nothing,
  which the route reads as "use the configured default" - i.e. *save*, the opposite of
  what the user asked for. The pair plus `getlist()` is what makes the form and the API
  both behave.
