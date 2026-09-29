"""Application factory for the Flask OCR service.

``create_app()`` is the single entry point used by ``run.py``, the Flask CLI and
the test suite, which keeps configuration in one place::

    from app import create_app

    app = create_app({"MAX_UPLOAD_MB": 5, "MAX_CONTENT_LENGTH": 5 * 1024 * 1024})
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask

from .config import Config
from .database import PAGES_TABLE_SUFFIX, DatabaseManager, MySqlSettings
from .error_handlers import register_error_handlers
from .exceptions import OcrAppError
from .ocr import TesseractEngine
from .ocr.images import configure_pillow_limits
from .routes import bp as main_blueprint
from .sqlite import DEFAULT_FILE_NAME as SQLITE_FILE_NAME
from .sqlite import SqliteSettings
from .storage import ResultStore


def create_app(config_overrides: dict | None = None) -> Flask:
    """Build and configure the Flask application."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    _configure_derived_settings(app)

    # The instance folder holds runtime state only: uploads are processed in
    # memory and never written to disk (the optional remembered MySQL connection and
    # the SQLite store live here too - see app/database.py and app/sqlite.py).
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    engine = TesseractEngine(
        app.config.get("TESSERACT_CMD"),
        languages=app.config["OCR_LANGUAGES"],
        psm=app.config["OCR_PSM"],
        oem=app.config["OCR_OEM"],
        timeout=app.config["OCR_TIMEOUT_SECONDS"],
    )
    app.extensions["ocr_engine"] = engine
    app.extensions["ocr_result_store"] = ResultStore(
        ttl_seconds=app.config["RESULT_TTL_SECONDS"],
        max_items=app.config["RESULT_CACHE_SIZE"],
    )
    database = DatabaseManager(
        defaults=_mysql_defaults(app.config),
        settings_file=app.config.get("MYSQL_SETTINGS_FILE")
        or Path(app.instance_path) / "mysql_connection.json",
        sqlite_defaults=_sqlite_defaults(app.config, app.instance_path),
        backend=app.config["DATABASE_BACKEND"],
    )
    app.extensions["ocr_database"] = database
    configure_pillow_limits(app.config["MAX_IMAGE_PIXELS"])

    app.register_blueprint(main_blueprint)
    register_error_handlers(app)
    _register_store_labels(app)

    _log_engine_state(app, engine)
    _connect_database_if_requested(app, database)
    return app


def _mysql_defaults(config) -> MySqlSettings:
    """Turn the ``MYSQL_*`` configuration into validated settings.

    Bad values raise :class:`~app.exceptions.InvalidDatabaseSettingsError`, which
    the app factory catches - a typo in an environment variable must not stop the
    OCR service from starting.
    """
    try:
        return MySqlSettings(
            host=config["MYSQL_HOST"],
            port=config["MYSQL_PORT"],
            user=config["MYSQL_USER"],
            password=config["MYSQL_PASSWORD"],
            database=config["MYSQL_DATABASE"],
            table=config["MYSQL_TABLE"],
            charset=config["MYSQL_CHARSET"],
            connect_timeout=config["MYSQL_CONNECT_TIMEOUT"],
        )
    except OcrAppError as exc:
        logging.getLogger(__name__).error("Ignoring the MySQL configuration: %s", exc.message)
        return MySqlSettings()


def _sqlite_defaults(config, instance_path: str) -> SqliteSettings:
    """Turn the ``SQLITE_*`` configuration into validated settings.

    ``SQLITE_PATH`` wins; without it the store is a file in the Flask instance
    folder (``instance/ocr_records.sqlite3`` - git-ignored, never served), right next
    to the remembered MySQL credentials.
    """
    path = config.get("SQLITE_PATH") or str(Path(instance_path) / SQLITE_FILE_NAME)
    table = config["SQLITE_TABLE"]
    try:
        return SqliteSettings(
            path=path,
            table=table,
            pages_table=f"{table}{PAGES_TABLE_SUFFIX}",
            timeout=config["SQLITE_TIMEOUT"],
        )
    except OcrAppError as exc:  # pragma: no cover - misconfiguration guard
        logging.getLogger(__name__).error("Ignoring the SQLite configuration: %s", exc.message)
        return SqliteSettings(path=path)


def _register_store_labels(app: Flask) -> None:
    """Expose the active store's names to every template as ``store``.

    ``store.label`` is ``MySQL`` or ``SQLite``, ``store.server_term`` finishes "No ...
    is connected" and ``store.server_phrase`` finishes "Connect to ...".  With
    ``DATABASE_BACKEND=auto`` and nothing connected that is MySQL, which is what the
    pages said before SQLite existed.
    """

    @app.context_processor
    def _store_labels() -> dict:
        return {"store": app.extensions["ocr_database"].labels}


def _connect_database_if_requested(app: Flask, database: DatabaseManager) -> None:
    """Opt-in auto-connect (``DATABASE_AUTO_CONNECT=1``) - never fatal.

    What is opened comes from ``DATABASE_BACKEND``: SQLite directly when it is
    ``sqlite``, MySQL otherwise - and in ``auto`` mode the local SQLite file when
    MySQL cannot be reached, so the service always has a working store.
    """
    if not app.config.get("DATABASE_AUTO_CONNECT"):
        app.logger.debug(
            "Database auto-connect is off; connect from the /database page when you need it."
        )
        return
    try:
        connected = database.auto_connect(remember=False)
    except OcrAppError as exc:
        app.logger.error("Database auto-connect failed: %s", exc.message)
    except Exception:  # pragma: no cover - defensive: start-up must survive anything
        app.logger.exception("Unexpected failure while auto-connecting to the database")
    else:
        app.logger.info(
            "%s ready: %s (schema %s, tables %s)",
            connected.provider,
            connected.settings.connection_label,
            "created" if connected.database_created else "already present",
            "created" if connected.tables_created else "already present",
        )


def _configure_derived_settings(app: Flask) -> None:
    """Values the templates and error handlers need but env vars do not provide."""
    extensions = app.config["ALLOWED_EXTENSIONS"]
    app.config["ALLOWED_EXTENSIONS_LABEL"] = ", ".join(
        sorted(ext.lstrip(".").upper() for ext in extensions)
    )


def _log_engine_state(app: Flask, engine: TesseractEngine) -> None:
    """Say loud and clear whether OCR can work, without paying for a probe."""
    if engine.command and engine.available:
        app.logger.info(
            "Tesseract OCR ready: %s (languages=%s, psm=%s, oem=%s)",
            engine.command,
            engine.languages,
            engine.psm,
            engine.oem,
        )
    else:
        app.logger.error(
            "Tesseract was not found, so OCR cannot run. Install it or set "
            "TESSERACT_CMD (see README.md). Current value: %r",
            engine.command or None,
        )


if __name__ == "__main__":  # pragma: no cover - convenience for `python -m app`
    logging.basicConfig(level=logging.INFO)
    create_app().run(port=5000, threaded=True)
