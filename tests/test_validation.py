"""Upload validation, error handling and API contract tests.

These never reach the OCR engine, so they run on any machine - with or without
Tesseract installed.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------
def test_index_renders_upload_form(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Drag &amp; drop your files here" in body
    assert 'name="file" multiple' in body, "a batch is one form submission"
    assert "Files per batch" in body
    assert "review them before anything is saved" in body


def test_only_the_store_panel_folds_away(client):
    """The upload explanation moved into the question mark: one accordion is left."""
    body = client.get("/").get_data(as_text=True)

    assert body.count('<details class="panel panel-muted accordion"') == 1
    assert body.count("<summary>") == 1, "the store panel is toggled by its own heading"
    assert "<h2>Database storage (MySQL)</h2>" in body
    assert not re.findall(r"<details[^>]*\bopen\b", body), "nothing is unfolded to start with"


def test_the_upload_explanation_hangs_off_the_question_mark(client):
    """*What happens to your file* is the popup of the ``?``, not a panel of its own."""
    body = client.get("/").get_data(as_text=True)

    assert '<div class="help-tip-popup" id="upload-help" role="tooltip">' in body
    assert 'aria-describedby="upload-help"' in body
    assert '<p class="help-tip-title">What happens to your file</p>' in body
    assert "Only after you confirm or correct them" in body, "the steps are still there"
    assert "Max PDF pages" in body and "OCR language" in body, "the limits are still there"


def test_health_endpoint_reports_engine(client):

    response = client.get("/api/health")
    assert response.status_code in (200, 503)
    payload = response.get_json()
    assert payload["engine"]["name"] == "tesseract"
    assert "allowed_extensions" in payload["limits"]


# ---------------------------------------------------------------------------
# validation failures
# ---------------------------------------------------------------------------
def test_upload_without_a_file_is_rejected(client):
    response = client.post("/upload", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Choose one or more JPG, PNG or PDF files" in response.get_data(as_text=True)



def test_unsupported_extension_is_rejected(upload_file, client):
    response = upload_file(client, b"hello", "notes.txt")
    assert response.status_code == 400
    assert ".txt is not supported" in response.get_data(as_text=True)


def test_empty_file_is_rejected(upload_file, client):
    response = upload_file(client, b"", "empty.png")
    assert response.status_code == 400
    assert "is empty" in response.get_data(as_text=True)


def test_fake_pdf_is_rejected_before_ocr(upload_file, client):
    """A .pdf extension on non-PDF bytes must not reach the OCR engine."""
    response = upload_file(client, b"this is definitely not a PDF", "invoice.pdf")
    assert response.status_code == 400
    assert "not a readable image or PDF" in response.get_data(as_text=True)


def test_pdf_bytes_uploaded_as_png_are_rejected(upload_file, client, text_pdf_factory):
    """Content wins over the extension, instead of OCR'ing the wrong thing."""
    response = upload_file(client, text_pdf_factory("hello"), "page.png")
    assert response.status_code == 400
    assert "contains a PDF document" in response.get_data(as_text=True)


def test_corrupt_image_is_rejected(upload_file, client):
    broken = b"\x89PNG\r\n\x1a\n" + b"garbage-that-is-not-a-real-png-chunk"
    response = upload_file(client, broken, "broken.png")
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "not a readable" in body or "damaged" in body


def test_oversized_upload_returns_413(upload_file, make_client, png_factory):
    client = make_client(MAX_UPLOAD_MB=0, MAX_CONTENT_LENGTH=512)
    response = upload_file(client, png_factory("too big for this test"), "big.png")
    assert response.status_code == 413
    assert "larger than the" in response.get_data(as_text=True)


def test_pdf_page_limit_is_enforced(upload_file, make_client, scanned_pdf_factory):
    """A 3 page PDF against a 2 page limit is rejected with a clear message."""
    client = make_client(
        MAX_UPLOAD_MB=8, MAX_CONTENT_LENGTH=8 * 1024 * 1024, MAX_PDF_PAGES=2
    )
    document = scanned_pdf_factory("page one", "page two", "page three")
    response = upload_file(client, document, "long.pdf")
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert "has 3 pages" in body and "limit is 2" in body


def test_unknown_result_id_returns_404(client):
    response = client.get("/result/" + "0" * 32)
    assert response.status_code == 404
    assert "no longer available" in response.get_data(as_text=True)


def test_wrong_method_is_reported(client):
    response = client.get("/upload")
    assert response.status_code == 405
    assert "not allowed" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# JSON API contract
# ---------------------------------------------------------------------------
def test_api_errors_are_json(client):
    response = client.post("/api/ocr", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_file"


def test_api_page_limit_error_is_json(upload_file, make_client, scanned_pdf_factory):
    client = make_client(
        MAX_UPLOAD_MB=8, MAX_CONTENT_LENGTH=8 * 1024 * 1024, MAX_PDF_PAGES=1
    )
    response = upload_file(
        client, scanned_pdf_factory("first page", "second page"), "two-pages.pdf", url="/api/ocr"
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "too_many_pages"


def test_unexpected_errors_are_handled(app, monkeypatch, upload_file):
    """A crash inside the pipeline becomes a friendly 500, not a stack trace."""
    app.config["PROPAGATE_EXCEPTIONS"] = False
    client = app.test_client()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("app.routes.extract_text", boom)
    response = upload_file(client, b"\x89PNG\r\n\x1a\n", "anything.png")
    assert response.status_code == 500
    assert "Something went wrong" in response.get_data(as_text=True)
