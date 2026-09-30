"""Upload every file in ``samples/`` through the real application and check it.

This is the script the sample files were accepted with: it builds the Flask app
exactly like ``run.py`` does (only pointed at a throw-away SQLite file), uploads
each sample through the **HTTP stack** (``POST /api/ocr``), and asserts what the
submission claims about the file:

* the right ``kind`` (``image`` / ``pdf``) and page count,
* the right extraction method per page (``ocr`` vs ``embedded`` - a born-digital
  PDF must never reach Tesseract),
* the expected fragments in the text (or *no* text at all for the blank page),
* that **every** upload was written to the store - verified twice, through
  ``GET /api/database/records`` and with plain ``sqlite3`` against the file.

Usage::

    env\\Scripts\\python.exe tools\\verify_samples.py
    env\\Scripts\\python.exe tools\\verify_samples.py --db instance/verify.sqlite3 --keep-db

Exit codes: ``0`` every check passed, ``1`` a check failed, ``2`` Tesseract is
not installed (the images and the scanned PDF cannot be OCR'd without it).
"""

from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


#: Importing the application (``app``) and the generator (``make_samples``)
#: needs both directories on ``sys.path`` - this script lives in ``tools/``.
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
for _directory in (str(REPO_ROOT), str(TOOLS_DIR)):
    if _directory not in sys.path:
        sys.path.insert(0, _directory)

from app import create_app  # noqa: E402  (import after sys.path is prepared)
from app.config import find_tesseract_cmd  # noqa: E402
from make_samples import SAMPLES, SAMPLES_DIR, Sample, write_samples  # noqa: E402


def normalise(value: object) -> str:
    """Lower case, letters and digits only - so OCR punctuation cannot fail a check."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def check_fields(sample: Sample, payload: dict) -> list[str]:
    """Every reason the structured fields do not match what *sample* promises."""
    expected_fields = sample.expected_fields
    if not expected_fields:
        return []
    fields = payload.get("fields") or {}
    problems: list[str] = []
    for key, expected in expected_fields.items():
        found = fields.get(key)
        if expected is None:
            if found:
                problems.append(f"field {key} is {found!r}, expected none")
        elif normalise(found) != normalise(expected):
            problems.append(f"field {key} is {found!r}, expected {expected!r}")
    return problems


def check_payload(sample: Sample, payload: dict) -> list[str]:
    """Every reason *payload* does not match what *sample* promises."""
    problems: list[str] = []

    if payload.get("kind") != sample.kind:
        problems.append(f"kind is {payload.get('kind')!r}, expected {sample.kind!r}")
    if payload.get("page_count") != sample.pages:
        problems.append(f"page_count is {payload.get('page_count')!r}, expected {sample.pages}")

    pages = payload.get("pages") or []
    wrong = [page.get("method") for page in pages if page.get("method") != sample.method]
    if wrong:
        problems.append(f"page method(s) {wrong} instead of {sample.method!r}")

    text = payload.get("text") or ""
    if sample.expected:
        missing = [f for f in sample.expected if f.lower() not in text.lower()]
        if missing:
            problems.append(f"text is missing {missing}: {text!r}")
    elif payload.get("char_count"):
        problems.append(f"expected an empty page, got {payload.get('char_count')} characters")

    problems.extend(check_fields(sample, payload))

    if not payload.get("database", {}).get("saved"):
        problems.append(f"not stored: {payload.get('database', {}).get('error')}")
    return problems



def stored_rows(db_path: Path) -> tuple[list[tuple], int]:
    """Read the store with plain ``sqlite3`` - no ORM, no application code.

    The per-page table is discovered (rather than hard-coded) as the one ending in
    ``_pages``, so the check keeps working if ``MYSQL_TABLE``/``SQLITE_TABLE`` is
    renamed.  The connection is closed explicitly: on Windows an open handle keeps
    the file locked, which would stop the temporary directory from being removed.
    """
    connection = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        pages_table = next((name for name in tables if name.endswith("_pages")), None)
        if pages_table is None:
            raise LookupError(f"no '*_pages' table in the store, only {tables}")
        extractions = connection.execute(
            "SELECT `id`, `filename`, `kind`, `page_count`, `char_count`, `content_sha256`, "
            "`supplier`, `invoice_number`, `total_amount`, `currency` "
            "FROM `ocr_extractions` ORDER BY `id`"
        ).fetchall()

        pages = connection.execute(f"SELECT COUNT(*) FROM `{pages_table}`").fetchone()[0]
    finally:
        connection.close()
    return extractions, pages


def main(argv: list[str] | None = None) -> int:
    """Run the checks; see the module docstring for the usage."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--samples", type=Path, default=SAMPLES_DIR, help="sample directory to use"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite file for the stored records (default: a temporary file)",
    )
    parser.add_argument(
        "--keep-db", action="store_true", help="keep the SQLite file (do not delete it)"
    )
    parser.add_argument(
        "--no-regenerate",
        action="store_true",
        help="use the files already on disk instead of rewriting them",
    )
    args = parser.parse_args(argv)

    command = find_tesseract_cmd()
    if not command:
        print(
            "Tesseract was not found: install it (README section 1) or set TESSERACT_CMD.\n"
            "Without the engine the images and the scanned PDF cannot be checked.",
            file=sys.stderr,
        )
        return 2
    print(f"OCR engine : {command}")

    if not args.no_regenerate:
        write_samples(args.samples)

    temporary: tempfile.TemporaryDirectory | None = None
    if args.db is not None:
        db_path = args.db
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="flask_ocr_verify_")
        db_path = Path(temporary.name) / "ocr_records.sqlite3"

    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "verify-samples",
            "TESSERACT_CMD": command,
            "DATABASE_BACKEND": "sqlite",
            "SQLITE_PATH": str(db_path),
        }
    )
    manager = application.extensions["ocr_database"]
    manager.connect_sqlite()
    client = application.test_client()
    print(f"Store      : {db_path}")
    print()

    header = (
        f"{'file':<40} {'HTTP':>5} {'kind':<6} {'pages':>5} {'extract':<9} "
        f"{'chars':>6} {'conf':>6} {'fields':>6} {'record':>7}  verdict"
    )

    print(header)
    print("-" * len(header))

    failures: list[str] = []
    try:
        for sample in SAMPLES:
            path = args.samples / sample.relative_path
            if not path.is_file():
                failures.append(f"{sample.relative_path}: file is missing")
                print(f"{sample.relative_path:<40}    --  (missing)")
                continue

            response = client.post(
                "/api/ocr",
                data={"file": (io.BytesIO(path.read_bytes()), sample.filename)},
                content_type="multipart/form-data",
            )
            if response.status_code != 200:
                payload = {}
                problems = [
                    f"HTTP {response.status_code}: {response.get_data(as_text=True)[:120]}"
                ]
            else:
                payload = response.get_json()
                problems = check_payload(sample, payload)

            if problems:
                failures.extend(f"{sample.relative_path}: {problem}" for problem in problems)
            confidence = payload.get("confidence")
            fields = payload.get("fields") or {}
            found = sum(1 for value in fields.values() if value)
            print(
                f"{sample.relative_path:<40} {response.status_code:>5} "
                f"{str(payload.get('kind', '-')):<6} {str(payload.get('page_count', '-')):>5} "
                f"{sample.method:<9} {str(payload.get('char_count', '-')):>6} "
                f"{(f'{confidence:.1f}' if confidence is not None else '-'):>6} "
                f"{(f'{found}/{len(fields)}' if fields else '-'):>6} "
                f"{str(payload.get('database', {}).get('record_id') or '-'):>7}  "
                f"{'ok' if not problems else 'FAIL'}"
            )


        # -- the store: the API and raw SQL must agree with what was uploaded
        listing = client.get("/api/database/records").get_json()
        rows, page_rows = stored_rows(db_path)
        expected_pages = sum(sample.pages for sample in SAMPLES)

        print()
        print(f"stored records        : {listing['query']['total']} (API) / {len(rows)} (SQLite)")
        print(f"stored pages          : {page_rows} (SQLite), expected {expected_pages}")
        for row in rows:
            print(
                f"  #{row[0]:<3} {row[1]:<34} {row[2]:<6} pages={row[3]} "
                f"chars={row[4]} fields=({row[6] or '-'}, {row[7] or '-'}, "
                f"{row[8] if row[8] is not None else '-'}, {row[9] or '-'}) "
                f"sha256={str(row[5])[:12]}"
            )


        if listing["query"]["total"] != len(SAMPLES) or len(rows) != len(SAMPLES):
            failures.append("the store does not hold one record per sample")
        if page_rows != expected_pages:
            failures.append(f"{page_rows} page rows stored, expected {expected_pages}")
        # Compare by file name, so the check does not depend on the upload order.
        empty_expected = {sample.filename for sample in SAMPLES if not sample.expected}
        empty_stored = {row[1] for row in rows if row[4] == 0}
        if empty_stored != empty_expected:
            failures.append(
                f"records without text are {sorted(empty_stored)}, "
                f"expected {sorted(empty_expected)}"
            )

    finally:
        manager.disconnect()
        if temporary is not None:
            if args.keep_db:
                print(f"\nSQLite file kept at {db_path}")
            else:
                temporary.cleanup()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(SAMPLES)} sample files passed every check.")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
