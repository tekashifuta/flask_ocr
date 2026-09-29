"""Tesseract OCR engine wrapper.

Only a very thin layer is added on top of :mod:`pytesseract`:

* the executable path / language / page-segmentation settings are resolved from
  the Flask config,
* Tesseract's TSV output is turned into plain text plus a mean confidence and a
  word count (one engine call per page, instead of text *and* data runs),
* process level failures are translated into :mod:`app.exceptions` so routes can
  answer with an actionable message (e.g. "Tesseract is not installed").
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

import pytesseract
from PIL import Image
from pytesseract import Output

from ..exceptions import OcrEngineUnavailableError, OcrProcessingError

logger = logging.getLogger(__name__)

#: Tesseract TSV ``level`` value for a single word.
WORD_LEVEL = 5

#: Serialises the ``pytesseract.tesseract_cmd`` assignment (module global state).
_CONFIG_LOCK = threading.Lock()
_CONFIGURED_COMMAND: str | None = None


@dataclass(frozen=True)
class OcrResult:
    """Text extracted from a single image, plus quality hints."""

    text: str
    confidence: float | None
    word_count: int
    duration_ms: int

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def _at(sequence: Any, index: int) -> Any:
    """Safe ``sequence[index]`` for the parallel TSV columns Tesseract returns."""
    if isinstance(sequence, (list, tuple)) and 0 <= index < len(sequence):
        return sequence[index]
    return None


def _as_int(value: Any, default: int = 0) -> int:
    """Tesseract returns numbers as strings when reading a TSV file."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def rebuild_text_from_tsv(data: Mapping[str, Any]) -> tuple[str, float | None, int]:
    """Convert ``pytesseract.image_to_data(..., output_type=DICT)`` into text.

    Returns ``(text, mean_confidence, word_count)``. Words are re-grouped into
    lines using Tesseract's block/paragraph/line numbers; confidence values of
    ``-1`` (Tesseract's "not a word" marker) are excluded from the mean.
    """
    texts = data.get("text") or []
    lines: list[str] = []
    current: list[str] = []
    current_key: tuple[Any, Any, Any] | None = None
    confidences: list[float] = []

    for index in range(len(texts)):
        if _as_int(_at(data.get("level"), index)) != WORD_LEVEL:
            continue

        key = (
            _at(data.get("block_num"), index),
            _at(data.get("par_num"), index),
            _at(data.get("line_num"), index),
        )
        if key != current_key and current:
            lines.append(" ".join(current))
            current = []
        current_key = key

        word = str(_at(texts, index) or "").strip()
        if not word:
            continue
        current.append(word)

        confidence = _as_float(_at(data.get("conf"), index))
        if confidence is not None and confidence >= 0:
            confidences.append(confidence)

    if current:
        lines.append(" ".join(current))

    text = "\n".join(lines).strip()
    mean_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None
    word_count = sum(len(line.split()) for line in lines)
    return text, mean_confidence, word_count


class TesseractEngine:
    """Configured handle to a Tesseract installation."""

    name = "tesseract"

    def __init__(
        self,
        command: str | None,
        *,
        languages: str = "eng",
        psm: int = 3,
        oem: int = 3,
        timeout: int = 120,
    ) -> None:
        self.command = (command or "").strip()
        self.languages = (languages or "eng").strip() or "eng"
        self.psm = int(psm)
        self.oem = int(oem)
        self.timeout = max(0, int(timeout))
        self._version: str | None = None
        self._apply_command()

    # -- configuration ----------------------------------------------------
    def _apply_command(self) -> None:
        """Point pytesseract at our executable (module level global state)."""
        global _CONFIGURED_COMMAND
        if not self.command:
            return
        with _CONFIG_LOCK:
            if _CONFIGURED_COMMAND != self.command:
                pytesseract.pytesseract.tesseract_cmd = self.command
                _CONFIGURED_COMMAND = self.command

    @property
    def available(self) -> bool:
        """Cheap existence check - does not launch Tesseract."""
        if not self.command:
            return False
        if os.path.isfile(self.command):
            return True
        return shutil.which(self.command) is not None

    def version(self) -> str | None:
        """Tesseract version string (``None`` when it cannot be executed)."""
        if self._version is not None:
            return self._version
        if not self.available:
            return None
        try:
            # pytesseract only caches the version when called with cached=True,
            # so calling it like this re-probes the binary on every cold start.
            self._version = str(pytesseract.get_tesseract_version())
        except SystemExit:  # pytesseract raises SystemExit for a bogus version
            logger.warning("Tesseract reported an unusable version string.")
            return None
        except (OSError, RuntimeError):
            return None
        return self._version

    def available_languages(self) -> list[str]:
        """Language packs installed next to the executable."""
        if not self.available:
            return []
        try:
            return sorted(pytesseract.get_languages(config=""))
        except (OSError, RuntimeError, SystemExit):
            return []

    def health(self) -> dict[str, Any]:
        """Snapshot used by ``/api/health`` and for start-up log messages."""
        version = self.version()
        return {
            "name": self.name,
            "available": bool(version),
            "command": self.command or None,
            "version": version,
            "languages": self.available_languages(),
            "requested_languages": self.languages,
            "psm": self.psm,
            "oem": self.oem,
            "timeout_seconds": self.timeout,
        }

    # -- OCR --------------------------------------------------------------
    def recognise(self, image: Image.Image) -> OcrResult:
        """Run Tesseract over *image* and return the text plus confidence."""
        if not self.available:
            raise OcrEngineUnavailableError(
                "Tesseract is not installed or could not be located. Install it "
                "(see README.md) or point the TESSERACT_CMD environment variable at it."
            )

        config = f"--oem {self.oem} --psm {self.psm}"
        started = time.perf_counter()
        try:
            data = pytesseract.image_to_data(
                image,
                lang=self.languages,
                config=config,
                output_type=Output.DICT,
                timeout=self.timeout,
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise OcrEngineUnavailableError(
                f"Tesseract could not be started at '{self.command}'. {exc}"
            ) from exc
        except RuntimeError as exc:
            # Covers both pytesseract.TesseractError and the timeout RuntimeError.
            detail = str(exc).strip() or exc.__class__.__name__
            raise OcrProcessingError(
                f"Tesseract could not process this document ({detail}). "
                "Try a higher resolution upload or a different page segmentation mode."
            ) from exc
        except OSError as exc:
            raise OcrEngineUnavailableError(f"Tesseract could not be started: {exc}") from exc

        text, confidence, word_count = rebuild_text_from_tsv(data)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "OCR finished in %sms (words=%s, confidence=%s, lang=%s, psm=%s)",
            duration_ms,
            word_count,
            confidence,
            self.languages,
            self.psm,
        )
        return OcrResult(
            text=text,
            confidence=confidence,
            word_count=word_count,
            duration_ms=duration_ms,
        )

