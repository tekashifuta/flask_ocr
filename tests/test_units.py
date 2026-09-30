"""Unit tests for the pieces that can be tested without a Tesseract binary.

They cover TSV to text reconstruction, the embedded-text decision, image
pre-processing, upload sniffing, formatting helpers and the result cache.
"""

from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest
from PIL import Image

import run

from app import create_app
from app.excel import (
    DECIMAL,
    INTEGER,
    MAX_CELL_CHARS,
    MAX_SHEET_NAME_CHARS,
    RECORD_COLUMNS,
    Column,
    Sheet,
    column_letter,
    records_workbook,
    workbook_bytes,
)
from app.exceptions import CorruptFileError
from app.ocr.documents import KIND_IMAGE, KIND_PDF, detect_kind, should_use_embedded_text
from app.ocr.engine import rebuild_text_from_tsv
from app.ocr.images import load_image, preprocess_for_ocr, to_data_uri
from app.storage import ResultStore
from app.utils import human_size

TSV_COLUMNS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def tsv(rows: list[tuple[int, int, int, int, object, str]]) -> dict:
    """Build the dict that ``pytesseract.image_to_data`` returns.

    Each row is ``(block, paragraph, line, level, confidence, text)`` and mixes
    ``int`` with ``str`` confidences on purpose: Tesseract's TSV reader hands
    back both depending on how the output is parsed.
    """
    data = {column: [] for column in TSV_COLUMNS}
    for block, paragraph, line, level, confidence, text in rows:
        data["level"].append(level)
        data["page_num"].append(1)
        data["block_num"].append(block)
        data["par_num"].append(paragraph)
        data["line_num"].append(line)
        data["left"].append(10)
        data["top"].append(10)
        data["width"].append(50)
        data["height"].append(20)
        data["conf"].append(confidence)
        data["text"].append(text)
    return data


# ---------------------------------------------------------------------------
# TSV -> text
# ---------------------------------------------------------------------------
def test_tsv_is_rebuilt_into_lines_with_confidence():
    data = tsv(
        [
            (1, 1, 1, 1, -1, ""),  # page row
            (1, 1, 1, 2, -1, ""),  # block row
            (1, 1, 1, 3, -1, ""),  # paragraph row
            (1, 1, 1, 4, -1, ""),  # line row
            (1, 1, 1, 5, 96, "Invoice"),
            (1, 1, 1, 5, "94", "2026"),
            (1, 1, 2, 5, 88, "Total"),
            (1, 1, 2, 5, 90, "1,234.56"),
        ]
    )

    text, confidence, word_count = rebuild_text_from_tsv(data)

    assert text == "Invoice 2026\nTotal 1,234.56"
    assert word_count == 4
    assert confidence == pytest.approx(92.0)


def test_tsv_ignores_negative_confidences_and_blank_words():
    data = tsv(
        [
            (1, 1, 1, 5, 80, "Alpha"),
            (1, 1, 1, 5, -1, "?"),  # "not a word" marker -> excluded from the mean
            (1, 1, 1, 5, 60, "   "),  # whitespace only -> skipped as text and score
        ]
    )

    text, confidence, word_count = rebuild_text_from_tsv(data)

    assert text == "Alpha ?"
    assert word_count == 2
    # Only "Alpha" has a usable score; the blank token is dropped entirely.
    assert confidence == pytest.approx(80.0)


def test_tsv_without_words_yields_empty_text():
    text, confidence, word_count = rebuild_text_from_tsv(tsv([(1, 1, 1, 1, -1, "")]))

    assert text == ""
    assert confidence is None
    assert word_count == 0


# ---------------------------------------------------------------------------
# embedded text decision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "min_chars", "expected"),
    [
        ("short", 50, False),
        ("x" * 50, 50, True),
        ("x" * 49, 50, False),
        ("", 50, False),
        ("   \n\n  ", 5, False),
        ("hello world", 5, True),
    ],
)
def test_should_use_embedded_text(text, min_chars, expected):
    assert should_use_embedded_text(text, min_chars) is expected


# ---------------------------------------------------------------------------
# upload sniffing
# ---------------------------------------------------------------------------
def test_detect_kind_accepts_images_and_pdfs(png_factory, text_pdf_factory):
    assert detect_kind(png_factory("hello"), "page.png") == KIND_IMAGE
    assert detect_kind(text_pdf_factory("hello"), "page.pdf") == KIND_PDF


def test_detect_kind_rejects_mismatched_content(png_factory, text_pdf_factory):
    with pytest.raises(CorruptFileError):
        detect_kind(text_pdf_factory("hello"), "page.png")
    with pytest.raises(CorruptFileError):
        detect_kind(png_factory("hello"), "page.pdf")


def test_detect_kind_rejects_unknown_bytes():
    with pytest.raises(CorruptFileError):
        detect_kind(b"neither an image nor a pdf", "mystery.png")


# ---------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------
def test_small_images_are_upscaled_and_greyscaled():
    image = Image.new("RGB", (300, 100), "white")

    prepared = preprocess_for_ocr(image, min_target_px=1800, max_upscale=3.0)

    assert prepared.mode == "L"
    assert max(prepared.size) == 900  # capped by max_upscale


def test_large_images_keep_their_size_but_become_grayscale():
    image = Image.new("RGB", (2500, 1200), "white")

    prepared = preprocess_for_ocr(image, min_target_px=1800, max_upscale=3.0)

    assert prepared.mode == "L"
    assert prepared.size == (2500, 1200)


def test_load_image_rejects_a_pixel_bomb():
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, format="PNG")

    with pytest.raises(CorruptFileError):
        load_image(buffer.getvalue(), max_pixels=1000)


def test_load_image_converts_to_rgb(png_factory):
    assert load_image(png_factory("hello")).mode == "RGB"


def test_to_data_uri_is_a_png_data_uri():
    uri = to_data_uri(Image.new("RGB", (2000, 500), "white"), max_px=200)

    assert uri.startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# formatting + cache
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (16 * 1024 * 1024, "16.0 MB"),
        (None, "0 B"),
    ],
)
def test_human_size(value, expected):
    assert human_size(value) == expected


def test_result_store_evicts_the_oldest_entry():
    store = ResultStore(ttl_seconds=60, max_items=2)
    first, second, third = object(), object(), object()

    first_id = store.put(first)
    second_id = store.put(second)
    third_id = store.put(third)

    assert store.get(first_id) is None, "the oldest result must be evicted"
    assert store.get(second_id) is second
    assert store.get(third_id) is third
    assert len(store) == 2


def test_result_store_expires_entries(monkeypatch):
    store = ResultStore(ttl_seconds=10, max_items=5)
    stored = object()
    result_id = store.put(stored)

    future = time.monotonic() + 11
    monkeypatch.setattr("app.storage.time.monotonic", lambda: future)

    assert store.get(result_id) is None
    assert len(store) == 0



# ---------------------------------------------------------------------------
# Excel export (the .xlsx writer behind /database/records/export.xlsx)
# ---------------------------------------------------------------------------
SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def workbook_parts(payload: bytes) -> dict[str, bytes]:
    """``{part: bytes}`` of a generated workbook, checking every part is XML."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    for name, content in parts.items():
        ElementTree.fromstring(content)  # raises when a part is not well formed
        assert content.startswith(b"<?xml"), f"{name} has no XML declaration"
    return parts


def sheet_cells(payload: bytes, index: int = 1) -> list[list[str | None]]:
    """Cell text/values of one sheet: ``[[A1, B1], [A2, B2], ...]``."""
    root = ElementTree.fromstring(workbook_parts(payload)[f"xl/worksheets/sheet{index}.xml"])
    rows: list[list[str | None]] = []
    for row in root.iter(f"{SPREADSHEET_NS}row"):
        cells: list[str | None] = []
        for cell in row.iter(f"{SPREADSHEET_NS}c"):
            inline = cell.find(f"{SPREADSHEET_NS}is/{SPREADSHEET_NS}t")
            numeric = cell.find(f"{SPREADSHEET_NS}v")
            cells.append(
                inline.text
                if inline is not None
                else (numeric.text if numeric is not None else None)
            )
        rows.append(cells)
    return rows


def sheet_names(payload: bytes) -> list[str]:
    """The tab names of a generated workbook, in order."""
    workbook = workbook_parts(payload)["xl/workbook.xml"].decode()
    return re.findall(r'<sheet name="([^"]*)"', workbook)


def test_column_letter_covers_the_spreadsheet_alphabet():
    assert column_letter(1) == "A"
    assert column_letter(26) == "Z"
    assert column_letter(27) == "AA"
    assert column_letter(52) == "AZ"
    assert column_letter(53) == "BA"
    assert column_letter(702) == "ZZ"
    assert column_letter(703) == "AAA"


def test_workbook_is_a_valid_opc_package():
    payload = workbook_bytes(
        [
            Sheet("First", (Column("A", width=10),), ((1,),)),
            Sheet("Second", (Column("B", width=12),), ((2,),)),
        ],
        title="Unit test",
        created=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
    )

    parts = workbook_parts(payload)

    assert list(parts) == [
        "[Content_Types].xml",
        "_rels/.rels",
        "docProps/app.xml",
        "docProps/core.xml",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet2.xml",
    ]
    assert 'PartName="/xl/worksheets/sheet2.xml"' in parts["[Content_Types].xml"].decode()
    rels = parts["xl/_rels/workbook.xml.rels"].decode()
    assert 'Target="worksheets/sheet1.xml"' in rels and 'Target="styles.xml"' in rels
    assert sheet_names(payload) == ["First", "Second"]
    assert 'r:id="rId2"' in parts["xl/workbook.xml"].decode()
    assert "2026-09-29T12:00:00Z" in parts["docProps/core.xml"].decode()



def test_workbook_writes_headers_text_and_numbers():
    payload = workbook_bytes(
        [
            Sheet(
                "Records",
                (
                    Column("File name", width=30),
                    Column("Pages", width=8, kind=INTEGER),
                    Column("Confidence (%)", width=12, kind=DECIMAL),
                    Column("Extracted text", width=60, wrap=True),
                ),
                (("123.pdf", 12, 93.25, "line one\nline two"),),
            )
        ]
    )

    assert sheet_cells(payload) == [
        ["File name", "Pages", "Confidence (%)", "Extracted text"],
        ["123.pdf", "12", "93.25", "line one\nline two"],
    ]
    sheet = workbook_parts(payload)["xl/worksheets/sheet1.xml"].decode()
    assert 't="inlineStr"' in sheet, "a name like 123.pdf stays text, not a number"
    assert "<v>12</v>" in sheet and "<v>93.25</v>" in sheet, "numbers stay numbers"
    assert 's="1"' in sheet, "the header row carries the header style"


def test_workbook_writes_values_excel_cannot_store_as_numbers_as_text():
    payload = workbook_bytes(
        [Sheet("S", (Column("value"),), ((float("nan"),), (float("inf"),)))]
    )

    assert [row[0] for row in sheet_cells(payload)] == ["value", "nan", "inf"]


def test_workbook_hides_none_and_empty_cells():
    payload = workbook_bytes(
        [Sheet("S", (Column("a"), Column("b"), Column("c")), ((None, "", "kept"),))]
    )

    assert sheet_cells(payload) == [["a", "b", "c"], [None, None, "kept"]]


def test_workbook_escapes_markup_and_drops_illegal_characters():
    payload = workbook_bytes(
        [
            Sheet(
                "S",
                (Column("text"),),
                (("A & B <tag>",), ("bell\x07 and\nform\x0cfeed",)),
            )
        ]
    )

    sheet = workbook_parts(payload)["xl/worksheets/sheet1.xml"].decode()

    assert "A &amp; B &lt;tag&gt;" in sheet
    assert "\x07" not in sheet and "\x0c" not in sheet, "XML 1.0 forbids those"
    assert sheet_cells(payload)[2][0] == "bell and\nformfeed"


def test_workbook_truncates_a_cell_to_excels_limit():
    payload = workbook_bytes([Sheet("S", (Column("text"),), (("x" * 40_000,),))])

    stored = sheet_cells(payload)[1][0]
    assert stored is not None
    assert len(stored) == MAX_CELL_CHARS
    assert stored.endswith("\u2026"), "the truncation is visible"


def test_workbook_sheet_names_are_legal_and_unique():
    payload = workbook_bytes(
        [
            Sheet("report/2026 [final]: v1?*", (Column("a"),), (("one",),)),
            Sheet("report/2026 [final]: v1?*", (Column("a"),), (("two",),)),
            Sheet("", (Column("a"),), (("three",),)),
        ]
    )

    names = sheet_names(payload)

    assert len(names) == 3 and len(set(names)) == 3, "duplicates are numbered"
    for name in names:
        assert 0 < len(name) <= MAX_SHEET_NAME_CHARS
        assert not set(name) & set("\\/*?:[]"), f"Excel rejects the name {name!r}"
    assert names[0].startswith("report 2026")
    assert names[-1] == "Sheet", "a nameless sheet still gets a name"


def test_workbook_freezes_the_header_and_filters_the_table():
    payload = workbook_bytes(
        [
            Sheet("S", (Column("a"), Column("b")), (("1", "2"), ("3", "4"))),
            Sheet("plain", (Column("a"),), (("1",),), freeze=False, autofilter=False),
        ]
    )

    frozen = workbook_parts(payload)["xl/worksheets/sheet1.xml"].decode()
    assert 'state="frozen"' in frozen and 'topLeftCell="A2"' in frozen
    assert '<autoFilter ref="A1:B3"/>' in frozen

    plain = workbook_parts(payload)["xl/worksheets/sheet2.xml"].decode()
    assert "frozen" not in plain and "autoFilter" not in plain



def test_records_workbook_lays_out_records_pages_and_filters():
    payload = records_workbook(
        [
            {
                "id": 1,
                "filename": "invoice.pdf",
                "supplier": "ACME GmbH",
                "invoice_number": "10042",
                "document_date": "2026-03-15",
                "total_amount": 128.5,
                "currency": "EUR",
                "uploaded_at": "2026-01-02 03:04:05 UTC",
                "kind": "pdf",
                "page_count": 2,
                "char_count": 40,
                "word_count": 7,
                "confidence": 93.25,
                "duration_ms": 250,
                "size_bytes": 2048,
                "ocr_language": "eng",
                "engine_version": "5.4.0",
                "stored_at": "2026-01-02 03:04:06 UTC",
                "content_sha256": "ab" * 32,
                "content": "ACME invoice total",
            }
        ],
        pages=[
            {
                "extraction_id": 1,
                "filename": "invoice.pdf",
                "page_number": 1,
                "method": "ocr",
                "char_count": 12,
                "word_count": 3,
                "confidence": 96.5,
                "duration_ms": 110,
                "content": "first page",
            }
        ],
        facts=[("Rows exported", 1)],
        title="Stored OCR records (1)",
    )

    assert sheet_names(payload) == ["Records", "Pages", "Export"]

    records = sheet_cells(payload, 1)
    assert records[0][:7] == [
        "#",
        "File name",
        "Supplier",
        "Invoice number",
        "Date",
        "Total amount",
        "Currency",
    ]
    assert records[0][7] == "Uploaded (UTC)"
    assert records[1][0] == "1" and records[1][1] == "invoice.pdf"
    assert records[1][2:7] == ["ACME GmbH", "10042", "2026-03-15", "128.5", "EUR"], (
        "the structured fields travel with the export"
    )
    assert records[1][-1] == "ACME invoice total", "the export carries the full text"


    pages = sheet_cells(payload, 2)
    assert pages[0][:4] == ["Record #", "File name", "Page", "Method"]
    assert pages[1][:4] == ["1", "invoice.pdf", "1", "ocr"]
    assert pages[1][-1] == "first page"

    assert ["Field", "Value"] in sheet_cells(payload, 3)
    assert ["Rows exported", "1"] in sheet_cells(payload, 3)


def test_records_workbook_omits_the_sheets_it_has_nothing_for():
    without_pages = records_workbook([{"id": 1, "filename": "a.pdf"}], facts=[("x", "y")])
    assert sheet_names(without_pages) == ["Records", "Export"]

    records_only = records_workbook([])
    assert sheet_names(records_only) == ["Records"]
    assert sheet_cells(records_only) == [[column.header for column in RECORD_COLUMNS]], (
        "an empty export is still a valid sheet with its headers"
    )


def test_workbook_bytes_are_reproducible():
    sheets = [Sheet("S", (Column("a"),), (("one",),))]
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert workbook_bytes(sheets, created=stamp) == workbook_bytes(sheets, created=stamp)


# ---------------------------------------------------------------------------
# development entry point (run.py)
# ---------------------------------------------------------------------------
def test_run_py_connects_a_store_at_start_up_by_default(monkeypatch):
    """``python run.py`` saves what it extracts, so the records view has data."""
    monkeypatch.delenv("DATABASE_AUTO_CONNECT", raising=False)
    monkeypatch.delenv("MYSQL_AUTO_CONNECT", raising=False)

    assert run.auto_connect_default() is True

    monkeypatch.setenv("DATABASE_AUTO_CONNECT", "0")
    assert run.auto_connect_default() is False
    monkeypatch.setenv("DATABASE_AUTO_CONNECT", "1")
    assert run.auto_connect_default() is True

    monkeypatch.delenv("DATABASE_AUTO_CONNECT", raising=False)
    monkeypatch.setenv("MYSQL_AUTO_CONNECT", "no")
    assert run.auto_connect_default() is False, "the older name still counts"
    monkeypatch.setenv("MYSQL_AUTO_CONNECT", "yes")
    assert run.auto_connect_default() is True


def test_run_py_says_where_the_records_go(caplog, tesseract_command):
    """The start-up log is what tells an operator "nothing is being saved"."""
    unconnected = create_app(
        {"TESTING": True, "SECRET_KEY": "testing", "TESSERACT_CMD": tesseract_command}
    )

    with caplog.at_level(logging.WARNING, logger="run"):
        run.report_store(unconnected)

    assert "reviewed data is NOT saved" in caplog.text
    assert "DATABASE_AUTO_CONNECT=1" in caplog.text, "and how to fix it"

    caplog.clear()
    connected = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "testing",
            "TESSERACT_CMD": tesseract_command,
            "DATABASE_BACKEND": "sqlite",
            "SQLITE_PATH": ":memory:",
        }
    )
    connected.extensions["ocr_database"].auto_connect(remember=False)

    with caplog.at_level(logging.INFO, logger="run"):
        run.report_store(connected)

    assert "Storing reviewed extractions in SQLite" in caplog.text
    assert "/database/records" in caplog.text, "it points at the records view"

