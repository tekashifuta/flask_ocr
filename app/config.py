"""Configuration for the Flask OCR application.

Every tunable is read from an environment variable so the same code can run on a
developer workstation, in CI or inside a container without code changes.
See ``README.md`` for the full list of settings.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .database import MAX_LIST_LIMIT

#: Project root (``flask_ocr/``) - used as the default instance path.
BASE_DIR = Path(__file__).resolve().parent.parent

#: Well known Tesseract locations, probed when ``TESSERACT_CMD`` is unset and no
#: ``tesseract`` executable can be found on ``PATH``.
TESSERACT_LOCATIONS = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    Path("/usr/bin/tesseract"),
    Path("/usr/local/bin/tesseract"),
    Path("/opt/homebrew/bin/tesseract"),
)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration guard
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration guard
        raise RuntimeError(f"{name} must be a number, got {raw!r}.") from exc


def _env_bool(name: str, default: bool) -> bool:
    """``1/true/yes/on`` (any case) enable a flag, ``0/false/no/off`` disable it."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean (true/false), got {raw!r}.")


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    """One of *choices* (case-insensitive), so a typo fails loudly at start-up."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in choices:
        raise RuntimeError(
            f"{name} must be one of {', '.join(choices)}, got {raw!r}."
        )
    return raw


def find_tesseract_cmd() -> str | None:
    """Locate the Tesseract executable.

    Resolution order: ``TESSERACT_CMD`` -> ``PATH`` -> well known install paths.
    ``None`` is returned when the engine is missing; the application then reports
    the problem through ``/api/health`` and a readable error page instead of
    crashing at import time.
    """
    override = os.environ.get("TESSERACT_CMD", "").strip()
    if override:
        return override

    on_path = shutil.which("tesseract")
    if on_path:
        return on_path

    for candidate in TESSERACT_LOCATIONS:
        if candidate.is_file():
            return str(candidate)

    return None


class Config:
    """Default (production-ish) configuration."""

    SECRET_KEY = _env_str("SECRET_KEY", "dev-secret-change-me")

    # --- uploads ---------------------------------------------------------
    MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 16)
    #: Hard request-body limit enforced by Werkzeug (answers ``413``).
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
    #: Extensions accepted from the upload form (case-insensitive).
    ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".pdf"})

    # --- OCR engine ------------------------------------------------------
    TESSERACT_CMD = find_tesseract_cmd()
    OCR_LANGUAGES = _env_str("OCR_LANGUAGES", "eng")
    OCR_PSM = _env_int("OCR_PSM", 3)
    OCR_OEM = _env_int("OCR_OEM", 3)
    OCR_TIMEOUT_SECONDS = _env_int("OCR_TIMEOUT_SECONDS", 120)

    # --- preprocessing / PDF rasterisation -------------------------------
    #: Rendering resolution for PDF pages sent to Tesseract.
    OCR_DPI = _env_int("OCR_DPI", 250)
    #: Images whose longest edge is below this are upscaled before OCR.
    OCR_MIN_TARGET_PX = _env_int("OCR_MIN_TARGET_PX", 1800)
    #: Upper bound for that upscaling, to keep tiny images from exploding.
    OCR_MAX_UPSCALE = _env_float("OCR_MAX_UPSCALE", 3.0)
    #: Safety valve so a huge poster page cannot exhaust memory while rendering.
    OCR_MAX_RENDER_PIXELS = _env_int("OCR_MAX_RENDER_PIXELS", 40_000_000)
    #: Pillow decompression-bomb guard for user supplied images.
    MAX_IMAGE_PIXELS = _env_int("MAX_IMAGE_PIXELS", 50_000_000)

    # --- PDF text/OCR decision ------------------------------------------
    #: Pages with at least this many embedded characters skip OCR entirely.
    MIN_EMBEDDED_TEXT_CHARS = _env_int("MIN_EMBEDDED_TEXT_CHARS", 50)
    #: Reject PDFs longer than this (guards against accidental 500-page scans).
    MAX_PDF_PAGES = _env_int("MAX_PDF_PAGES", 25)

    # --- result presentation --------------------------------------------
    PREVIEW_MAX_PX = _env_int("PREVIEW_MAX_PX", 360)
    RESULT_TTL_SECONDS = _env_int("RESULT_TTL_SECONDS", 1800)
    RESULT_CACHE_SIZE = _env_int("RESULT_CACHE_SIZE", 50)

    # --- MySQL persistence ----------------------------------------------
    #: Pre-filled values for the ``/database`` connection form. Nothing is
    #: contacted until an operator connects (or ``MYSQL_AUTO_CONNECT`` is set).
    MYSQL_HOST = _env_str("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = _env_int("MYSQL_PORT", 3306)
    MYSQL_USER = _env_str("MYSQL_USER", "root")
    #: Not stripped - passwords may legitimately contain spaces.
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
    #: Schema (database) that is created on connect when it does not exist yet.
    MYSQL_DATABASE = _env_str("MYSQL_DATABASE", "flask_ocr")
    MYSQL_TABLE = _env_str("MYSQL_TABLE", "ocr_extractions")
    MYSQL_CHARSET = _env_str("MYSQL_CHARSET", "utf8mb4")
    MYSQL_CONNECT_TIMEOUT = _env_int("MYSQL_CONNECT_TIMEOUT", 8)
    #: Connect at start-up using the environment/saved credentials.
    MYSQL_AUTO_CONNECT = _env_bool("MYSQL_AUTO_CONNECT", False)
    #: Store every successful extraction while connected (opt out per upload).
    MYSQL_AUTO_SAVE = _env_bool("MYSQL_AUTO_SAVE", True)
    #: Remember the credentials used in the UI (``instance/mysql_connection.json``).
    MYSQL_REMEMBER_SETTINGS = _env_bool("MYSQL_REMEMBER_SETTINGS", True)
    #: Override the file used to remember the connection (defaults to the instance folder).
    MYSQL_SETTINGS_FILE = _env_str("MYSQL_SETTINGS_FILE", "") or None
    #: Rows shown per page in the records table (``/database`` and
    #: ``/database/records``); the view and the JSON API both accept ``?limit=``
    #: (clamped to :data:`~app.database.MAX_LIST_LIMIT`) to narrow a page and
    #: ``?page=`` to walk the rest.
    #:
    #: The default is that same cap, so the records view lists **every** stored
    #: record on one page whenever the store holds up to 200 rows - an empty store
    #: shows its empty-state row instead.  Set it lower (``MYSQL_RECORDS_LIMIT=10``)
    #: to page by default again, or pick a page size in the toolbar's
    #: **Rows per page** selector.
    MYSQL_RECORDS_LIMIT = _env_int("MYSQL_RECORDS_LIMIT", MAX_LIST_LIMIT)

    # --- storage backend -------------------------------------------------
    #: Which store the Database page connects to: ``auto`` (MySQL, and SQLite when
    #: MySQL cannot be reached at start-up), ``mysql`` or ``sqlite``.  SQLite needs
    #: no server at all, which is what makes the whole storage path (saving,
    #: searching, the records view, the Excel export) usable on a machine - or in a
    #: test run - that has no MySQL.
    DATABASE_BACKEND = _env_choice("DATABASE_BACKEND", "auto", ("auto", "mysql", "sqlite"))
    #: Connect at start-up using the environment/saved credentials.  With ``auto``
    #: the local SQLite file is used when MySQL is not available, so the service
    #: always has a store.  ``MYSQL_AUTO_CONNECT`` is the older name and still works.
    DATABASE_AUTO_CONNECT = _env_bool(
        "DATABASE_AUTO_CONNECT", _env_bool("MYSQL_AUTO_CONNECT", False)
    )
    #: Store every successful extraction while a store is connected (opt out per
    #: upload); ``MYSQL_AUTO_SAVE`` is the older name and still works.
    DATABASE_AUTO_SAVE = _env_bool("DATABASE_AUTO_SAVE", _env_bool("MYSQL_AUTO_SAVE", True))

    # --- SQLite fallback (one file, no server) ---------------------------
    #: File the SQLite backend uses; empty means ``<instance>/ocr_records.sqlite3``.
    #: ``:memory:`` gives a throw-away database - handy for tests.
    SQLITE_PATH = _env_str("SQLITE_PATH", "") or None
    #: Table with one row per upload (its per-page table is ``<table>_pages``).
    SQLITE_TABLE = _env_str("SQLITE_TABLE", "ocr_extractions")
    #: Seconds the connection waits for a lock held by another process.
    SQLITE_TIMEOUT = _env_int("SQLITE_TIMEOUT", 8)

    @property
    def allowed_extensions_label(self) -> str:
        """Human readable extension list for the UI, e.g. ``JPG, JPEG, PNG, PDF``."""
        return ", ".join(sorted(ext.lstrip(".").upper() for ext in self.ALLOWED_EXTENSIONS))
