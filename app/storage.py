"""In-memory store for completed extractions.

Deliberately simple and dependency free: extracted text is kept for a short TTL
so the result page can be re-opened and the ``.txt`` download can be served
without re-running OCR. Nothing is ever written to disk - uploaded documents are
processed in memory and discarded with the request.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict

from .ocr import ExtractionResult

logger = logging.getLogger(__name__)


class ResultStore:
    """Thread safe, size and time bounded cache of :class:`ExtractionResult`."""

    def __init__(self, ttl_seconds: int = 1800, max_items: int = 50) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_items = max(1, int(max_items))
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, tuple[float, ExtractionResult]]" = OrderedDict()

    def put(self, result: ExtractionResult) -> str:
        """Store *result* and return its lookup id."""
        result_id = uuid.uuid4().hex
        with self._lock:
            self._purge_locked()
            self._items[result_id] = (time.monotonic(), result)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return result_id

    def get(self, result_id: str) -> ExtractionResult | None:
        """Return a still valid result, or ``None`` when unknown/expired."""
        with self._lock:
            self._purge_locked()
            entry = self._items.get(result_id)
            return entry[1] if entry else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _purge_locked(self) -> None:
        deadline = time.monotonic() - self.ttl_seconds
        expired = [key for key, (stored_at, _) in self._items.items() if stored_at < deadline]
        for key in expired:
            self._items.pop(key, None)
        if expired:
            logger.debug("Evicted %s expired OCR result(s)", len(expired))
