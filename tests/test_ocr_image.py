"""End-to-end tests for image uploads.

Every test here needs a working Tesseract installation and is skipped otherwise.
"""

from __future__ import annotations

import io
import re

from PIL import Image

RESULT_ID_PATTERN = re.compile(r"/result/([0-9a-f]{32})")


def test_png_upload_is_ocred(upload_file, client, png_factory, requires_tesseract):
    image = png_factory("Invoice number 2026 total 1234.56 USD")
    response = upload_file(client, image, "invoice.png")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Invoice" in body
    assert "1234.56" in body
    assert "Text layer" not in body  # an image has no text layer, it is always OCR'd


def test_result_page_can_be_reopened_and_downloaded(
    upload_file, client, png_factory, requires_tesseract
):
    response = upload_file(client, png_factory("Reference code ABC123"), "note.png")
    match = RESULT_ID_PATTERN.search(response.get_data(as_text=True))
    assert match, "the result page must link to the stored extraction"
    result_id = match.group(1)

    reopened = client.get(f"/result/{result_id}")
    assert reopened.status_code == 200
    assert "ABC123" in reopened.get_data(as_text=True)

    download = client.get(f"/result/{result_id}/download")
    assert download.status_code == 200
    assert download.mimetype == "text/plain"
    assert "attachment" in download.headers["Content-Disposition"]
    assert "ABC123" in download.get_data(as_text=True)


def test_api_ocr_returns_structured_json(
    upload_file, client, png_factory, requires_tesseract
):
    response = upload_file(
        client, png_factory("Purchase order 2026 XY"), "order.png", url="/api/ocr"
    )
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["kind"] == "image"
    assert payload["page_count"] == 1
    assert payload["word_count"] >= 3
    assert payload["confidence"] > 0
    assert payload["engine"]["version"], "the Tesseract version must be reported"
    assert payload["engine"]["languages"] == "eng"

    page = payload["pages"][0]
    assert page["page_number"] == 1
    assert page["method"] == "ocr"
    assert "Purchase" in page["text"]
    assert payload["download_url"].startswith("/result/")


def test_blank_image_reports_no_text(upload_file, client, requires_tesseract):
    """A page without any ink must come back with an explicit "no text" notice."""
    buffer = io.BytesIO()
    Image.new("RGB", (900, 300), "white").save(buffer, format="PNG")

    response = upload_file(client, buffer.getvalue(), "blank.png")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "No text could be detected" in body
    assert "No text detected on this page." in body
