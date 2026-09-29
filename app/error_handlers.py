"""Turn exceptions into consistent HTML pages or JSON bodies.

Browsers get a rendered page they can act on; ``/api`` clients (and any request
that explicitly asks for JSON) get a machine readable ``{"ok": false, ...}``.
"""

from __future__ import annotations

import logging

from flask import Flask, current_app, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .exceptions import OcrAppError
from .utils import human_size, upload_limits, wants_json

logger = logging.getLogger(__name__)


def register_error_handlers(app: Flask) -> None:
    """Attach handlers for domain errors, HTTP errors and unexpected failures."""

    @app.errorhandler(OcrAppError)
    def _handle_domain_error(error: OcrAppError):
        log = logger.error if error.status_code >= 500 else logger.info
        log("%s (%s): %s", error.code, error.status_code, error.message)
        return _render_error(error.message, error.status_code, error.code)

    @app.errorhandler(HTTPException)
    def _handle_http_error(error: HTTPException):
        status = error.code or 500
        if status >= 500:
            logger.error("HTTP %s while serving %s", status, request.path)
        return _render_error(
            _http_message(error), status, (error.name or "http_error").lower().replace(" ", "_")
        )

    @app.errorhandler(Exception)
    def _handle_unexpected_error(error: Exception):
        logger.exception("Unhandled %s while serving %s", type(error).__name__, request.path)
        return _render_error(
            "Something went wrong while processing that document. Please try again.",
            500,
            "internal_error",
        )


def _http_message(error: HTTPException) -> str:
    """Friendly text for the framework generated errors we can trigger."""
    if error.code == 413:
        limit = current_app.config.get("MAX_CONTENT_LENGTH")
        allowed = current_app.config.get("ALLOWED_EXTENSIONS_LABEL", "")
        if limit:
            return (
                f"That upload is larger than the {human_size(limit)} limit. "
                f"Allowed types: {allowed}."
            )
    if error.code == 404:
        return "That page does not exist. Start from the upload form to extract a document."
    if error.code == 405:
        return f"{request.method} is not allowed on this URL."
    return error.description or "The request could not be completed."


def _render_error(message: str, status: int, code: str):
    if wants_json():
        return jsonify({"ok": False, "error": {"code": code, "message": message}}), status
    return (
        render_template(
            "error.html",
            error=message,
            error_code=code,
            status_code=status,
            limits=upload_limits(),
        ),
        status,
    )
