# Testing against a real database file: shared SQLite fixtures

Answering the question "for testing only, what are my database options?" the choice
was option 2 - **a real SQLite database file in pytest's `tmp_path`**. `sqlite3`
ships with Python, so that store needs no server, no credentials, no install and no
network, and it is what `tests/test_sqlite.py` had already been doing by hand
(`make_client(DATABASE_BACKEND=BACKEND_SQLITE, SQLITE_PATH=str(tmp_path / "x.sqlite3"))`).

Nothing new had to be written on the storage side, so the work was to turn that
pattern into **reusable fixtures**: any test can now ask for a real, already-connected
SQLite database instead of repeating the connect boilerplate.

## What was added

### `tests/conftest.py` - three fixtures (and the isolation rule)

| Fixture | What it gives a test |
|---|---|
| `sqlite_path` | `tmp_path / "ocr_records.sqlite3"` - the `Path`; the file is created when a store connects |
| `sqlite_app` | `create_app({DATABASE_BACKEND: "sqlite", SQLITE_PATH: <that path>})` with `connect_sqlite()` already called; teardown clears the result cache and disconnects |
| `sqlite_client` / `sqlite_store` | That app's test client / the live `SqliteDatabase` |

Each test gets its **own** file, deleted by pytest afterwards, so no test can see
another test's rows. The connect is explicit rather than `DATABASE_AUTO_CONNECT`,
both because a test must not depend on that default and because the file only appears
when a store opens it - which lets a test assert that connecting is what created it.

### `tests/test_sqlite.py` - the fixtures are covered (215 → **217**)

* `test_sqlite_client_fixture_uploads_into_the_tmp_path_file` - the file exists as
  soon as the fixture connected, a `POST /upload` answers "Saved to SQLite as ...",
  the records view lists the row, and it is read back **straight out of the file with
  plain `sqlite3`**, no application code in the way.
* `test_sqlite_store_fixture_is_the_live_store_in_the_tmp_path_file` - the store is
  the SQLite provider pointed at the fixture's path, saving works without an HTTP
  round trip (`save_extraction` → id `1`, `record_count()` `1`), and the row is
  visible to `sqlite3`.

`tests/test_sqlite.py` alone is now **45** tests (`tests/test_database.py` stays at
111). **Verified:** `217 passed in 5.14s`, no MySQL server, no network, Windows /
Python 3.14.

### Docs - README §9

A new **Testing against a real database file** subsection: the fixture table, a
copy-paste example that reads its assertion back with plain `sqlite3`, and the rule of
thumb - use these fixtures for storage behaviour, and keep `tests/test_database.py`'s
`FakeConnection` for what is **MySQL-dialect** specific (`%s` placeholders, backticked
identifiers, `LEFT()`, `ESCAPE '!'`), which SQLite spells differently and so cannot
stand in for.

## Limitations to note

* The fixtures deliberately do **not** use `SQLITE_PATH=:memory:`: an in-memory
  database is empty again after a reconnect, so a test that connects twice - or that
  asserts the file on disk - needs the `tmp_path` file.
* This is the right store for the *storage path* (real SQL, real transactions, real
  cascades), not for MySQL *server* behaviour (charsets, permissions, collation): no
  amount of SQLite will tell you how a real MySQL instance would answer.
