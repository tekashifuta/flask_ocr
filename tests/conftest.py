"""Shared pytest fixtures.

Test documents are generated in memory (PNG pages from a TrueType font, PDFs
from Pillow's PDF writer plus a hand written text-layer PDF), so the suite needs
no binary assets and no network access.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import create_app
from app.config import find_tesseract_cmd

#: First font that exists wins; keeps the suite portable across OSes.
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)

PAGE_SIZE = (1200, 400)


def _font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)  # Pillow >= 10.1


def render_text_png(text: str, *, size: tuple[int, int] = PAGE_SIZE, font_size: int = 56) -> bytes:
    """A white page with crisp black text - what a scanned document looks like."""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((40, 40), text, font=_font(font_size), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_scanned_pdf(*page_texts: str) -> bytes:
    """Multi-page image-only PDF: the output of a document scanner."""
    pages = [Image.open(io.BytesIO(render_text_png(text))) for text in page_texts]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


def build_text_pdf(text: str) -> bytes:
    """Single page PDF carrying a real (selectable) text layer - no OCR needed."""
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    content = f"BT /F1 14 Tf 72 700 Td ({escaped}) Tj ET\n".encode("latin-1", "replace")

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


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def tesseract_command() -> str | None:
    """Resolved Tesseract executable (``None`` when it is not installed)."""
    return find_tesseract_cmd()


@pytest.fixture(scope="session")
def requires_tesseract(tesseract_command: str | None) -> str:
    """Skip a test when no OCR engine is available on this machine."""
    if not tesseract_command:
        pytest.skip("Tesseract is not installed - see README.md (install step 2).")
    return tesseract_command


@pytest.fixture()
def app(tesseract_command):
    """Application under test with deliberately small limits."""
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "testing",
            "MAX_UPLOAD_MB": 2,
            "MAX_CONTENT_LENGTH": 2 * 1024 * 1024,
            "TESSERACT_CMD": tesseract_command,
            "RESULT_TTL_SECONDS": 60,
            # Lower than the production default (250) purely to keep the suite fast.
            "OCR_DPI": 150,
        }
    )
    yield application
    application.extensions["ocr_result_store"].clear()


@pytest.fixture()
def client(app):
    """Flask test client bound to :func:`app`."""
    return app.test_client()


@pytest.fixture()
def make_client(tesseract_command):
    """Factory building clients with extra config overrides (page/size limits)."""
    created = []

    def _make(**overrides):
        config = {"TESTING": True, "SECRET_KEY": "testing", "TESSERACT_CMD": tesseract_command}
        config.update(overrides)
        application = create_app(config)
        created.append(application)
        return application.test_client()

    yield _make
    for application in created:
        application.extensions["ocr_result_store"].clear()


# ---------------------------------------------------------------------------
# testing against a real database file (no server, nothing to install)
# ---------------------------------------------------------------------------
# ``sqlite3`` is part of CPython, so these give every test its own *real* database
# in ``tmp_path`` - the whole storage path (schema, save, search, records view,
# Excel export, delete) runs offline, and nothing leaks between tests.  Prefer
# them over a stub when the SQL/transaction behaviour is what you are testing.
@pytest.fixture()
def sqlite_path(tmp_path) -> Path:
    """Path of a fresh SQLite database; the file is created when a store connects."""
    return tmp_path / "ocr_records.sqlite3"


@pytest.fixture()
def sqlite_app(sqlite_path, tesseract_command):
    """The app under test wired to that file and **already connected**.

    Connecting is explicit because the file only appears when a store opens it -
    a test may assert that connecting is what created it - and because a test must
    never depend on ``DATABASE_AUTO_CONNECT``.
    """
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "testing",
            "TESSERACT_CMD": tesseract_command,
            "DATABASE_BACKEND": "sqlite",
            "SQLITE_PATH": str(sqlite_path),
        }
    )
    application.extensions["ocr_database"].connect_sqlite()
    yield application
    application.extensions["ocr_result_store"].clear()
    application.extensions["ocr_database"].disconnect()


@pytest.fixture()
def sqlite_client(sqlite_app):
    """Flask test client bound to :func:`sqlite_app` - uploads land in that file."""
    return sqlite_app.test_client()


@pytest.fixture()
def sqlite_store(sqlite_app):
    """The live ``SqliteDatabase`` behind :func:`sqlite_app` (direct, no HTTP)."""
    return sqlite_app.extensions["ocr_database"].database


@pytest.fixture()
def upload_file():
    """``upload_file(client, data, filename, url=...) -> response``.

    Mimics the browser multipart POST: ``client.post(url, data={"file": ...})``.
    """

    def _upload(client, data: bytes, filename: str, url: str = "/upload"):
        return client.post(
            url,
            data={"file": (io.BytesIO(data), filename)},
            content_type="multipart/form-data",
        )

    return _upload


@pytest.fixture()
def png_factory():
    """``png_factory("text") -> bytes`` - a synthetic scanned page."""
    return render_text_png


@pytest.fixture()
def scanned_pdf_factory():
    """``scanned_pdf_factory("page 1", "page 2") -> bytes`` - image-only PDF."""
    return build_scanned_pdf


@pytest.fixture()
def text_pdf_factory():
    """``text_pdf_factory("text") -> bytes`` - PDF with a selectable text layer."""
    return build_text_pdf

