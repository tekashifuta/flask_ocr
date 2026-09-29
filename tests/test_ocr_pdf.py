"""End-to-end tests for PDF uploads: scanned, digital (text layer) and multi-page.

The OCR tests need Tesseract; ``test_pdf_with_a_text_layer_needs_no_ocr`` does
not, because that page never reaches the engine - which is exactly the point.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ocr


def test_multi_page_scanned_pdf_is_ocred_page_by_page(
    upload_file, client, scanned_pdf_factory, requires_tesseract
):
    """A 3 page image-only PDF (a scanner) must yield 3 OCR'd pages."""
    document = scanned_pdf_factory(
        "ALPHA PAGE ONE CONTENT",
        "BRAVO PAGE TWO CONTENT",
        "CHARLIE PAGE THREE CONTENT",
    )
    response = upload_file(client, document, "scan.pdf", url="/api/ocr")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["kind"] == "pdf"
    assert payload["page_count"] == 3
    assert [page["page_number"] for page in payload["pages"]] == [1, 2, 3]
    assert all(page["method"] == "ocr" for page in payload["pages"])

    assert "ALPHA" in payload["pages"][0]["text"]
    assert "BRAVO" in payload["pages"][1]["text"]
    assert "CHARLIE" in payload["pages"][2]["text"]

    # Multi-page output is delimited so the page context survives copy/paste.
    assert "----- Page 2 of 3 -----" in payload["text"]


def test_pdf_with_a_text_layer_needs_no_ocr(upload_file, client, text_pdf_factory):
    """A born-digital PDF is read straight from its embedded text layer."""
    document = text_pdf_factory(
        "This digital page already contains a selectable text layer, so no OCR is needed."
    )
    response = upload_file(client, document, "digital.pdf", url="/api/ocr")
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["page_count"] == 1
    page = payload["pages"][0]
    assert page["method"] == "embedded"
    assert page["confidence"] is None
    assert "selectable text layer" in page["text"]


def test_pdf_first_page_preview_is_embedded_in_the_page(
    upload_file, client, scanned_pdf_factory, requires_tesseract
):
    response = upload_file(client, scanned_pdf_factory("preview me"), "one-page.pdf")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "data:image/png;base64," in body
    assert "First page preview" in body


def test_single_page_pdf_has_no_page_marker(upload_file, client, text_pdf_factory):
    document = text_pdf_factory("Only one page here, with plenty of selectable characters.")
    response = upload_file(client, document, "single.pdf", url="/api/ocr")
    payload = response.get_json()
    assert "----- Page" not in payload["text"]
