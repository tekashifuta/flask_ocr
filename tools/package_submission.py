"""Build the single submission ZIP: source + SQL scripts + samples + README.

Run it from the project root with the virtual environment's interpreter::

    env\\Scripts\\python.exe tools\\package_submission.py
    env\\Scripts\\python.exe tools\\package_submission.py --output dist\\flask_ocr.zip

The archive holds **one** top-level folder (``flask_ocr/``) with

* the complete application source (``app/``, ``run.py``, ``requirements*.txt``,
  ``pyproject.toml``),
* the test suite (``tests/``),
* the database scripts (``sql/mysql_schema.sql``, ``sql/sqlite_schema.sql``),
* the sample files used for testing (``samples/``),
* the README (``README.md``) and the development notes (``notes/``).

Runtime state is deliberately left out - a checkout's virtual environment
(``env/``), the git history (``.git/``), the Flask instance folder with the
SQLite files and the remembered credentials (``instance/``) and every
``__pycache__``.  After writing the archive it is re-opened to check that it is
intact and really contains the four required kinds of deliverable.
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

#: Project root (``flask_ocr/``) - the folder that ends up inside the archive.
REPO_ROOT = Path(__file__).resolve().parent.parent
#: Name of the single top-level folder in the archive.
ARCHIVE_FOLDER = "flask_ocr"
#: Where the archive is written unless ``--output`` says otherwise.
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "flask_ocr_submission.zip"

#: Never packed: virtual environments, history, runtime state, caches, editors.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "instance",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        ".vscode",
        ".idea",
    }
)
#: Never packed: byte code, database files, generated downloads, OS junk.
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".sqlite3", ".sqlite", ".db", ".ocr.txt", ".db.txt")
EXCLUDED_NAMES = frozenset({".coverage", "Thumbs.db", "desktop.ini"})

#: Deliverables that must be in the archive (checked after writing it).
REQUIRED_PATHS = (
    "README.md",
    "run.py",
    "requirements.txt",
    "app/routes.py",
    "app/fields.py",
    "app/review.py",
    "tests/conftest.py",
    "tests/test_fields.py",
    "tests/test_review.py",
    "sql/mysql_schema.sql",
    "sql/sqlite_schema.sql",
    "samples/images/scan_invoice.png",
    "samples/images/scan_invoice_fields.png",
    "samples/images/scan_receipt.jpg",
    "samples/pdf/scanned_invoice_3_pages.pdf",
    "samples/pdf/digital_report_text_layer.pdf",
)



def _is_excluded(path: Path) -> bool:
    """Whether *path* (relative to the project root) stays out of the archive."""
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    if path.name in EXCLUDED_NAMES:
        return True
    return path.name.endswith(EXCLUDED_SUFFIXES)


def collect_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every file of the project that belongs in the archive, sorted by name."""
    files = [
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and not _is_excluded(path.relative_to(root))
    ]
    return sorted(files, key=lambda item: str(item).lower())


def build_archive(output: Path, root: Path = REPO_ROOT) -> tuple[Path, list[str], int]:
    """Write the archive and return ``(path, names, total_bytes)``."""
    output.parent.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    total = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in collect_files(root):
            arcname = f"{ARCHIVE_FOLDER}/{relative.as_posix()}"
            archive.write(root / relative, arcname)
            names.append(arcname)
            total += (root / relative).stat().st_size
    return output, names, total


def verify_archive(output: Path) -> list[str]:
    """Re-open the archive: integrity, folder prefix and required deliverables."""
    problems: list[str] = []
    with zipfile.ZipFile(output) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            problems.append(f"the archive is corrupt at {corrupt!r}")
        contents = archive.namelist()

    for required in REQUIRED_PATHS:
        if f"{ARCHIVE_FOLDER}/{required}" not in contents:
            problems.append(f"{required} is missing from the archive")

    stray = [name for name in contents if not name.startswith(f"{ARCHIVE_FOLDER}/")]
    if stray:
        problems.append(f"entries outside {ARCHIVE_FOLDER}/: {stray[:3]}")
    if not any(name.count("/") > 1 for name in contents):
        problems.append("the archive has no sub folders - is it the right tree?")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Build the archive, report what went in and verify it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="archive to write"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="do not list every packed file"
    )
    args = parser.parse_args(argv)

    output, names, total = build_archive(args.output)

    if not args.quiet:
        for name in names:
            print(name)
        print()
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"{len(names)} file(s), {total:,} bytes of source packed into {output.name}")
    print(f"archive: {output}")
    print(f"         {output.stat().st_size:,} bytes, sha256 {digest}")

    problems = verify_archive(output)
    if problems:
        print()
        print(f"{len(problems)} problem(s) with the archive:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"verified: {len(REQUIRED_PATHS)} required deliverables present, archive "
        f"intact, single top-level folder {ARCHIVE_FOLDER}/"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())

