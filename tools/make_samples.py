"""Generate the binary sample files that ship in ``samples/``.

The test suite builds its documents **in memory** (``tests/conftest.py``), which
keeps the repository free of binary fixtures.  This script is the same idea in
reverse: it writes the very same shapes of document to disk so the submission has
real files to upload - a photo-style JPG, a crisp PNG scan, an image-only 3 page
PDF (the scanner case, every page needs OCR), a born-digital PDF (text layer, no
OCR) and a blank page (the "no text found" case).

Run it from the project root with the virtual environment's interpreter::

    env\\Scripts\\python.exe tools\\make_samples.py

Nothing is downloaded and no font has to be installed: a TrueType font from the
operating system is used, with Pillow's bundled default font as the last resort.

``tools/verify_samples.py`` imports :data:`SAMPLES` from this module and uploads
every generated file through the real application, so the files and the
expectations can never drift apart.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

#: Project root (``flask_ocr/``) - the samples are written next to it.
REPO_ROOT = Path(__file__).resolve().parent.parent
#: Where the generated files land.
SAMPLES_DIR = REPO_ROOT / "samples"

#: First font that exists wins; keeps the script portable across OSes.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

#: Page geometry: ~300 DPI worth of text on a scanner-sized page.
PAGE_SIZE = (1200, 400)
FONT_SIZE = 48
LINE_HEIGHT = 62
MARGIN = 40



#: The text that goes into every sample (single source of truth for the checks).
INVOICE_LINES = ("ACME invoice 2026", "Invoice no: 10042", "Total: 128.50 EUR")
RECEIPT_LINES = ("Corner Coffee", "Latte 3.50 EUR", "Thanks for your visit")
#: An invoice that carries every structured field *with a label*: the sample for
#: the review page (supplier, invoice number, date, total, currency).
DETAILED_INVOICE_LINES = (
    "NORTHWIND TRADING GMBH",
    "Supplier: Northwind Trading GmbH",
    "Invoice no: INV-2026-0042",
    "Date: 15.03.2026",
    "Subtotal: 118.00 EUR",
    "VAT 19%: 22.42 EUR",
    "Total: 140.42 EUR",
)
#: What the parser must read out of ``DETAILED_INVOICE_LINES`` - the values the
#: review page shows and the database stores.
DETAILED_INVOICE_FIELDS = {
    "supplier": "Northwind Trading GmbH",
    "invoice_number": "INV-2026-0042",
    "document_date": "2026-03-15",
    "total_amount": "140.42",
    "currency": "EUR",
}
#: Tall enough for those seven lines: the default page would clip the "Total" line,
#: and a scanner does not invent what it never saw.
DETAILED_INVOICE_SIZE = (
    PAGE_SIZE[0],
    MARGIN * 2 + len(DETAILED_INVOICE_LINES) * LINE_HEIGHT,
)

SCANNED_PDF_PAGES = (
    ("ACME purchase order ALPHA", "Line one of the scanned document"),
    ("ACME purchase order BRAVO", "Line two of the scanned document"),
    ("ACME purchase order CHARLIE", "Line three of the scanned document"),
)
DIGITAL_PDF_LINES = (
    "Quarterly report Q1 2026",
    "Revenue 1,240,000 EUR",
    "Growth 12.4 %",
    "Prepared by the finance team.",
)



def _font(size: int = FONT_SIZE):
    """A TrueType font at *size*, or Pillow's default font when none is found."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)  # Pillow >= 10.1


def render_text_image(
    lines: Sequence[str],
    *,
    fmt: str = "PNG",
    size: tuple[int, int] = PAGE_SIZE,
    font_size: int = FONT_SIZE,
) -> bytes:
    """A white page with crisp black text - what a flatbed scanner produces."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = _font(font_size)
    for index, line in enumerate(lines):
        draw.text((MARGIN, MARGIN + index * LINE_HEIGHT), line, font=font, fill="black")
    buffer = io.BytesIO()
    if fmt.upper() == "JPEG":
        image.save(buffer, format="JPEG", quality=90)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def render_blank_png(size: tuple[int, int] = PAGE_SIZE) -> bytes:
    """An empty page: the upload succeeds and yields zero characters."""
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def build_scanned_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """Multi-page image-only PDF - the output of a document scanner.

    Every page is a picture of text and therefore has no text layer, so the
    application has to rasterise each page and run Tesseract on it.
    """
    images = [Image.open(io.BytesIO(render_text_image(lines))) for lines in pages]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def build_text_pdf(lines: Sequence[str]) -> bytes:
    """Single page PDF carrying a real (selectable) text layer - no OCR needed.

    Hand written on purpose: the document is a few hundred bytes, needs no
    dependency beyond the standard library, and is exactly the shape of a
    "born digital" report exported by an office suite.
    """

    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    parts = ["BT /F1 14 Tf 72 700 Td"]
    for index, line in enumerate(lines):
        if index:
            parts.append("0 -18 Td")
        parts.append(f"({escape(line)}) Tj")
    parts.append("ET")
    content = (" ".join(parts) + "\n").encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"endstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    startxref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{startxref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


@dataclass(frozen=True)
class Sample:
    """One sample file: where it goes, what it is, what the OCR must find."""

    #: Path relative to ``samples/`` (always with forward slashes).
    relative_path: str
    #: ``image`` or ``pdf`` - the ``kind`` the application reports.
    kind: str
    #: One line for the README / the verification report.
    description: str
    #: Fragments that must appear in the extracted text (case-insensitive).
    expected: tuple[str, ...]
    #: Callable producing the bytes.
    build: Callable[[], bytes] = lambda: b""  # replaced per entry below
    #: Expected number of pages (1 for images).
    pages: int = 1
    #: ``ocr`` or ``embedded`` - how each page must be extracted.
    method: str = "ocr"
    #: Structured fields (``app/fields.py``) the document must yield, when the
    #: sample is meant to exercise the review page.
    expected_fields: Mapping[str, str | None] | None = None

    @property
    def filename(self) -> str:
        """Base name of the file."""
        return self.relative_path.rsplit("/", 1)[-1]



SAMPLES: tuple[Sample, ...] = (
    Sample(
        relative_path="images/scan_invoice.png",
        kind="image",
        description="Crisp 1200x400 PNG scan of a three line invoice (the flatbed scanner case).",
        expected=("ACME", "10042", "128.50"),
        build=lambda: render_text_image(INVOICE_LINES),
        method="ocr",
        # Only the invoice number and the total carry a label; the supplier is the
        # header line, and there is no date on that page at all.
        expected_fields={
            "supplier": "ACME",
            "invoice_number": "10042",
            "document_date": None,
            "total_amount": "128.50",
            "currency": "EUR",
        },
    ),
    Sample(
        relative_path="images/scan_invoice_fields.png",
        kind="image",
        description=(
            "Invoice with every structured field labelled: supplier, invoice number, "
            "date, subtotal, VAT and total (the review page case)."
        ),
        expected=("NORTHWIND", "INV-2026-0042", "140.42", "22.42"),
        build=lambda: render_text_image(DETAILED_INVOICE_LINES, size=DETAILED_INVOICE_SIZE),
        method="ocr",
        expected_fields=dict(DETAILED_INVOICE_FIELDS),

    ),

    Sample(
        relative_path="images/scan_receipt.jpg",
        kind="image",
        description="JPEG photograph-style receipt (compression, .jpg code path).",
        expected=("Corner Coffee", "Latte", "3.50"),
        build=lambda: render_text_image(RECEIPT_LINES, fmt="JPEG"),
        method="ocr",
    ),
    Sample(
        relative_path="images/blank_page.png",
        kind="image",
        description="Blank white page: the 'no text found' path (0 characters, no error).",
        expected=(),
        build=render_blank_png,
        method="ocr",
    ),
    Sample(
        relative_path="pdf/scanned_invoice_3_pages.pdf",
        kind="pdf",
        description="Image-only 3 page PDF: every page has to be rasterised and OCR'd.",
        expected=("ALPHA", "BRAVO", "CHARLIE", "----- Page 2 of 3 -----"),
        build=lambda: build_scanned_pdf(SCANNED_PDF_PAGES),
        pages=3,
        method="ocr",
    ),
    Sample(
        relative_path="pdf/digital_report_text_layer.pdf",
        kind="pdf",
        description="Born-digital PDF with a selectable text layer: read directly, never OCR'd.",
        expected=("Quarterly report Q1 2026", "1,240,000", "finance team"),
        build=lambda: build_text_pdf(DIGITAL_PDF_LINES),
        method="embedded",
    ),
)


def write_samples(out_dir: Path = SAMPLES_DIR) -> list[tuple[Sample, Path, bytes]]:
    """Write every sample under *out_dir* and return ``(sample, path, bytes)``."""
    written: list[tuple[Sample, Path, bytes]] = []
    for sample in SAMPLES:
        data = sample.build()
        path = out_dir / sample.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append((sample, path, data))
    return written


def _display(path: Path) -> str:
    """Path relative to the project root when possible, else the absolute path."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:  # pragma: no cover - --out pointed outside the checkout
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Write the samples and print a manifest (path, size, digest, expectation)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=SAMPLES_DIR,
        help=f"directory to write into (default: {SAMPLES_DIR})",
    )
    args = parser.parse_args(argv)

    written = write_samples(args.out)
    print(f"{len(written)} sample file(s) written to {args.out}")
    print()
    header = f"{'file':<44} {'bytes':>8}  {'sha256[:16]':<16} pages  extract"
    print(header)
    print("-" * len(header))
    for sample, path, data in written:
        print(
            f"{_display(path):<44} {len(data):>8}  "
            f"{hashlib.sha256(data).hexdigest()[:16]:<16} "
            f"{sample.pages:>5}  {sample.method}"
        )
    print()
    print("Upload them through the UI (/) or POST them to /api/ocr, e.g.")
    print(f"  curl -F \"file=@{SAMPLES_DIR / SAMPLES[0].relative_path}\" \\")
    print("       http://127.0.0.1:5000/api/ocr")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())

