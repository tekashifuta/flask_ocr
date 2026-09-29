"""Turn uploaded bytes into text pages.

Two input families are supported:

``image``
    A single JPG/PNG page that is pre-processed and sent to Tesseract.

``pdf``
    A PDF of any length. Each page is inspected individually: pages that already
    carry a text layer (digital/"born digital" PDFs) are read directly, while
    pages without one (scans, photos) are rasterised with PDFium and OCR'd. This
    hybrid keeps digital PDFs instant *and* works for pure scans and mixed
    documents, and it needs no external helper binaries such as Poppler.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from ..exceptions import (
    CorruptFileError,
    EmptyFileError,
    EncryptedPdfError,
    PageLimitExceededError,
)
from . import images
from .engine import TesseractEngine

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
KIND_IMAGE = "image"
KIND_PDF = "pdf"
METHOD_OCR = "ocr"
METHOD_EMBEDDED = "embedded"

#: PDF points per inch - PDFium renders in canvas units of 1/72 inch.
PDF_POINTS_PER_INCH = 72.0
#: Scale used for the cheap first-page thumbnail of a PDF.
PREVIEW_RENDER_SCALE = 1.0


@dataclass(frozen=True)
class PageResult:
    """Extraction outcome for one page (a page == the whole image for uploads)."""

    page_number: int
    text: str
    method: str
    confidence: float | None = None
    duration_ms: int = 0

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def was_ocred(self) -> bool:
        return self.method == METHOD_OCR


@dataclass(frozen=True)
class ExtractionResult:
    """Everything the UI needs to present one processed upload."""

    filename: str
    kind: str
    pages: tuple[PageResult, ...]
    duration_ms: int
    languages: str
    tesseract_version: str | None = None
    preview_data_uri: str | None = None
    size_bytes: int = 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocred_page_count(self) -> int:
        return sum(1 for page in self.pages if page.was_ocred)

    @property
    def embedded_page_count(self) -> int:
        return self.page_count - self.ocred_page_count

    @property
    def char_count(self) -> int:
        return sum(page.char_count for page in self.pages)

    @property
    def word_count(self) -> int:
        return sum(page.word_count for page in self.pages)

    @property
    def is_empty(self) -> bool:
        return self.char_count == 0

    @property
    def confidence(self) -> float | None:
        """Mean confidence across the pages that were OCR'd."""
        values = [
            page.confidence
            for page in self.pages
            if page.was_ocred and page.confidence is not None
        ]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def full_text(self) -> str:
        """All pages as one string, separated by page markers for multi-page PDFs."""
        if self.page_count == 1:
            return self.pages[0].text
        blocks = [
            f"----- Page {page.page_number} of {self.page_count} -----\n\n{page.text}"
            for page in self.pages
        ]
        return "\n\n".join(blocks)


def detect_kind(data: bytes, filename: str) -> str:
    """Classify the upload from its *content*, not just its extension.

    Rejects files whose bytes contradict their extension, which is the usual
    outcome of a renamed or truncated download.
    """
    suffix = Path(filename).suffix.lower()

    if PDF_MAGIC in data[:1024]:
        actual = KIND_PDF
    elif images.looks_like_image(data):
        actual = KIND_IMAGE
    else:
        actual = None

    if actual is None:
        raise CorruptFileError(
            f"'{filename}' is not a readable image or PDF document. "
            "Upload a JPG/PNG image or a PDF file."
        )
    if suffix == ".pdf" and actual != KIND_PDF:
        raise CorruptFileError(
            f"'{filename}' has a .pdf extension but contains an image, not a PDF."
        )
    if suffix != ".pdf" and actual != KIND_IMAGE:
        raise CorruptFileError(
            f"'{filename}' has a {suffix or 'unknown'} extension but contains a PDF document."
        )
    return actual


def should_use_embedded_text(text: str, min_chars: int) -> bool:
    """Decide whether a PDF page already contains enough selectable text."""
    return len((text or "").strip()) >= max(1, min_chars)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def extract_text(
    data: bytes,
    filename: str,
    *,
    engine: TesseractEngine,
    dpi: int = 250,
    max_pdf_pages: int = 25,
    min_embedded_chars: int = 50,
    min_target_px: int = 1800,
    max_upscale: float = 3.0,
    max_render_pixels: int = 40_000_000,
    preview_max_px: int = 360,
    max_image_pixels: int = 50_000_000,
) -> ExtractionResult:
    """Extract text from an uploaded image or PDF.

    Raises:
        EmptyFileError, UnsupportedFileTypeError, CorruptFileError,
        EncryptedPdfError, PageLimitExceededError,
        OcrEngineUnavailableError, OcrProcessingError.
    """
    if not data:
        raise EmptyFileError("The uploaded file is empty.")

    kind = detect_kind(data, filename)
    started = time.perf_counter()

    if kind == KIND_PDF:
        pages, preview = _extract_pdf_pages(
            data,
            engine,
            dpi=dpi,
            max_pdf_pages=max_pdf_pages,
            min_embedded_chars=min_embedded_chars,
            min_target_px=min_target_px,
            max_upscale=max_upscale,
            max_render_pixels=max_render_pixels,
            preview_max_px=preview_max_px,
        )
    else:
        pages, preview = _extract_image_page(
            data,
            engine,
            min_target_px=min_target_px,
            max_upscale=max_upscale,
            preview_max_px=preview_max_px,
            max_image_pixels=max_image_pixels,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    result = ExtractionResult(
        filename=filename,
        kind=kind,
        pages=pages,
        duration_ms=duration_ms,
        languages=engine.languages,
        tesseract_version=engine.version(),
        preview_data_uri=preview,
        size_bytes=len(data),
    )
    logger.info(
        "Extracted %s from %r in %sms (%s page(s), %s chars, %s OCR page(s))",
        kind,
        filename,
        duration_ms,
        result.page_count,
        result.char_count,
        result.ocred_page_count,
    )
    return result


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def _extract_image_page(
    data: bytes,
    engine: TesseractEngine,
    *,
    min_target_px: int,
    max_upscale: float,
    preview_max_px: int,
    max_image_pixels: int,
) -> tuple[tuple[PageResult, ...], str | None]:
    """Process a single uploaded JPG/PNG."""
    image = images.load_image(data, max_pixels=max_image_pixels)
    prepared = images.preprocess_for_ocr(
        image, min_target_px=min_target_px, max_upscale=max_upscale
    )
    outcome = engine.recognise(prepared)
    page = PageResult(
        page_number=1,
        text=outcome.text,
        method=METHOD_OCR,
        confidence=outcome.confidence,
        duration_ms=outcome.duration_ms,
    )
    return (page,), images.to_data_uri(image, max_px=preview_max_px)


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------
def _extract_pdf_pages(
    data: bytes,
    engine: TesseractEngine,
    *,
    dpi: int,
    max_pdf_pages: int,
    min_embedded_chars: int,
    min_target_px: int,
    max_upscale: float,
    max_render_pixels: int,
    preview_max_px: int,
) -> tuple[tuple[PageResult, ...], str | None]:
    """Walk every page of a PDF, OCR-ing only the ones that need it."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise EncryptedPdfError(
                "This PDF is password protected, so its text cannot be extracted."
            ) from exc
        raise CorruptFileError(
            f"The PDF could not be opened ({exc}). It may be damaged or was "
            "produced by an unsupported tool."
        ) from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise CorruptFileError("The PDF does not contain any pages.")
        if page_count > max_pdf_pages:
            raise PageLimitExceededError(
                f"This PDF has {page_count} pages but the limit is {max_pdf_pages} "
                "per upload. Split the document and try again."
            )

        preview = _render_preview(document[0], preview_max_px)
        pages = tuple(
            _extract_pdf_page(
                document,
                index,
                engine,
                dpi=dpi,
                min_embedded_chars=min_embedded_chars,
                min_target_px=min_target_px,
                max_upscale=max_upscale,
                max_render_pixels=max_render_pixels,
            )
            for index in range(page_count)
        )
        return pages, preview
    finally:
        document.close()


def _extract_pdf_page(
    document: "pdfium.PdfDocument",
    index: int,
    engine: TesseractEngine,
    *,
    dpi: int,
    min_embedded_chars: int,
    min_target_px: int,
    max_upscale: float,
    max_render_pixels: int,
) -> PageResult:
    """Extract one page: embedded text layer when present, OCR otherwise."""
    page_number = index + 1
    page = document[index]

    embedded = _read_embedded_text(page)
    if should_use_embedded_text(embedded, min_embedded_chars):
        logger.debug(
            "Page %s: reusing the embedded text layer (%s chars)", page_number, len(embedded)
        )
        return PageResult(
            page_number=page_number,
            text=embedded,
            method=METHOD_EMBEDDED,
            confidence=None,
            duration_ms=0,
        )

    logger.debug("Page %s: no usable text layer, falling back to OCR", page_number)
    image = _render_page(page, dpi=dpi, max_render_pixels=max_render_pixels)
    prepared = images.preprocess_for_ocr(
        image, min_target_px=min_target_px, max_upscale=max_upscale
    )
    outcome = engine.recognise(prepared)
    return PageResult(
        page_number=page_number,
        text=outcome.text,
        method=METHOD_OCR,
        confidence=outcome.confidence,
        duration_ms=outcome.duration_ms,
    )


def _read_embedded_text(page: "pdfium.PdfPage") -> str:
    """Return the selectable text of *page* (an empty string for pure images).

    ``get_text_range()`` is used rather than ``get_text_bounded()`` because the
    latter clips results to the page's MediaBox - text that a generator placed
    (slightly) outside it would be silently dropped.
    """
    textpage = page.get_textpage()
    try:
        return (textpage.get_text_range() or "").strip()
    finally:
        textpage.close()


def _render_page(
    page: "pdfium.PdfPage",
    *,
    dpi: int,
    max_render_pixels: int,
) -> Image.Image:
    """Rasterise *page* at *dpi*, staying inside the pixel budget.

    ``scale`` is expressed in PDF canvas units (1/72 inch), hence the ``dpi/72``
    conversion. Oversized pages are clamped so a giant sheet cannot exhaust
    memory before Tesseract ever runs.
    """
    width, height = page.get_size()
    if width <= 0 or height <= 0:  # pragma: no cover - malformed page geometry
        raise CorruptFileError("A page in this PDF has no usable dimensions.")

    scale = max(0.1, dpi / PDF_POINTS_PER_INCH)
    if max_render_pixels > 0:
        budget_scale = (max_render_pixels / (width * height)) ** 0.5
        if scale > budget_scale:
            logger.debug(
                "Clamping render scale %.2f -> %.2f to respect the pixel budget",
                scale,
                budget_scale,
            )
            scale = max(0.1, budget_scale)

    bitmap = page.render(scale=scale, rotation=0)
    try:
        # convert("RGB") always detaches the pixels from PDFium's own buffer,
        # so the image stays valid after bitmap.close().
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def _render_preview(page: "pdfium.PdfPage", preview_max_px: int) -> str | None:
    """Low resolution thumbnail of the first page (purely cosmetic)."""
    bitmap = None
    try:
        bitmap = page.render(scale=PREVIEW_RENDER_SCALE, rotation=0)
        return images.to_data_uri(bitmap.to_pil(), max_px=preview_max_px)
    except Exception as exc:  # noqa: BLE001 - no thumbnail must not fail the upload
        logger.debug("Skipping the PDF preview: %s", exc)
        return None
    finally:
        if bitmap is not None:
            bitmap.close()

