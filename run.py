"""Development entry point.

Usage::

    python run.py                  # http://127.0.0.1:5000
    python run.py --port 8001
    python run.py --debug          # auto reload + Flask debugger

Started this way the app **connects a store before it takes traffic** - the local
SQLite file unless MySQL is configured and reachable (see ``DATABASE_BACKEND``) - so
what you extract is really saved and the records view has something to show. Set
``DATABASE_AUTO_CONNECT=0`` (or the older ``MYSQL_AUTO_CONNECT=0``) to connect by hand
from ``/database`` instead.

Every other OCR setting is read from the environment - see README.md for the list.
"""

from __future__ import annotations

import argparse
import logging
import os

from app import create_app

logger = logging.getLogger(__name__)

#: Auto-connect values that mean "no".
_FALSEY = {"0", "false", "no", "off"}


def auto_connect_default() -> bool:
    """Whether this entry point should connect a store at start-up (default: yes).

    ``run.py`` is what a developer or a demo starts, and an unconnected service
    silently keeps nothing: uploads only live in the in-memory result cache, so the
    records view stays empty.  An explicit ``DATABASE_AUTO_CONNECT`` (or the older
    ``MYSQL_AUTO_CONNECT``) in the environment still wins, which is how you get the
    connect-by-hand behaviour back.
    """
    for name in ("DATABASE_AUTO_CONNECT", "MYSQL_AUTO_CONNECT"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw.lower() not in _FALSEY
    return True


def report_store(app) -> None:
    """Log where the extracted text is being kept, or how to get a store."""
    manager = app.extensions["ocr_database"]
    if manager.is_connected:
        status = manager.status()
        settings = status.get("settings") or {}
        logger.info(
            "Storing extractions in %s (%s) - browse them at /database/records",
            manager.labels.label,
            status.get("connection_label") or settings.get("label") or "?",
        )
        return
    logger.warning(
        "No store is connected, so uploads are NOT saved (they only live in the "
        "in-memory result cache). Open /database to connect one, or set "
        "DATABASE_AUTO_CONNECT=1 - with SQLITE_PATH for a local SQLite file."
    )


def main() -> None:  # pragma: no cover - manual entry point
    parser = argparse.ArgumentParser(description="Run the Flask OCR web application.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.environ.get("FLASK_DEBUG") == "1",
        help="enable the reloader and the interactive debugger",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    app = create_app(
        {"DEBUG": args.debug, "DATABASE_AUTO_CONNECT": auto_connect_default()}
    )
    report_store(app)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

