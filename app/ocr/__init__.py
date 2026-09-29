"""OCR package: image pre-processing, the Tesseract wrapper and the pipeline."""

from .documents import (
    KIND_IMAGE,
    KIND_PDF,
    METHOD_EMBEDDED,
    METHOD_OCR,
    ExtractionResult,
    PageResult,
    detect_kind,
    extract_text,
    should_use_embedded_text,
)
from .engine import OcrResult, TesseractEngine, rebuild_text_from_tsv

__all__ = [
    "KIND_IMAGE",
    "KIND_PDF",
    "METHOD_EMBEDDED",
    "METHOD_OCR",
    "ExtractionResult",
    "OcrResult",
    "PageResult",
    "TesseractEngine",
    "detect_kind",
    "extract_text",
    "rebuild_text_from_tsv",
    "should_use_embedded_text",
]
