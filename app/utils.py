"""Small presentation helpers shared by routes, templates and error handlers."""

from __future__ import annotations

from flask import current_app, request

#: Bytes -> human readable, e.g. ``16.0 MB``.
_SIZE_UNITS = ("B", "KB", "MB", "GB")


def human_size(num_bytes: int | None) -> str:
    """Format a byte count for display."""
    if not num_bytes or num_bytes < 0:
        return "0 B"
    size = float(num_bytes)
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - unreachable


def wants_json() -> bool:
    """True when the caller should receive a JSON error instead of an HTML page.

    The ``/api`` prefix always wins so an API client can never end up parsing an
    HTML error page; browsers (``Accept: text/html``) keep getting pages.
    """
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best == "application/json"


def human_duration(seconds: int | None) -> str:
    """Format a duration for display, e.g. ``30 minutes`` or ``45 seconds``."""
    if not seconds or seconds < 0:
        return "0 seconds"
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    remainder = minutes % 60
    label = f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{label} {remainder} min" if remainder else label


def upload_limits(config=None) -> dict:
    """Facts about the accepted input, rendered in the UI sidebar."""
    config = config if config is not None else current_app.config
    limit = config.get("MAX_CONTENT_LENGTH")
    ttl = config.get("RESULT_TTL_SECONDS")
    return {
        "allowed_extensions": config.get("ALLOWED_EXTENSIONS_LABEL", "JPG, JPEG, PNG, PDF"),
        "max_upload_bytes": limit,
        "max_upload": human_size(limit) if limit else "unlimited",
        "max_batch_files": config.get("MAX_BATCH_FILES"),
        "max_pdf_pages": config.get("MAX_PDF_PAGES"),
        "languages": config.get("OCR_LANGUAGES"),
        "dpi": config.get("OCR_DPI"),
        "result_ttl_seconds": ttl,
        "result_ttl": human_duration(ttl),
    }

