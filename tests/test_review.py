"""Tests for the review step and batch upload (``app/review.py`` + the routes).

Two groups:

* **pure** tests of the review model and the form parsing (no OCR, no database);
* **end-to-end** tests of the flow ``POST /upload`` -> review page ->
  ``POST /review/save`` against a real SQLite file, plus the two JSON endpoints
  that let a script do the same thing.

Text-layer PDFs are used wherever a test does not need Tesseract, so most of the
flow is covered even on a machine without the engine.
"""

from __future__ import annotations

import io
import re
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from app.fields import (
    FIELD_SUPPLIER,
    FIELD_TOTAL_AMOUNT,
    extract_fields,
    validate_fields,
)

from app.ocr import ExtractionResult, PageResult
from app.review import (
    ReviewBatch,
    ReviewDocument,
    checkbox_value,
    documents_from,
    parse_form,
    submitted_field_values,
    submitted_result_ids,
)

INVOICE_TEXT = "ACME invoice 2026\nInvoice no: 10042\nTotal: 128.50 EUR"

#: The hidden field each review card carries - how a test finds the cached results.
RESULT_ID = re.compile(r'name="result_id" value="([0-9a-f]{32})"')



# ---------------------------------------------------------------------------
# the model and the form parsing (no OCR, no store)
# ---------------------------------------------------------------------------
def extraction(text: str = INVOICE_TEXT, filename: str = "invoice.pdf") -> ExtractionResult:
    """A finished extraction, fields included - what the pipeline returns."""
    return ExtractionResult(
        filename=filename,
        kind="pdf",
        pages=(PageResult(page_number=1, text=text, method="embedded"),),
        duration_ms=5,
        languages="eng",
        fields=extract_fields(text, filename=filename),
    )


def review_document(text: str = INVOICE_TEXT, **overrides) -> ReviewDocument:
    result = extraction(text)
    return ReviewDocument(
        result_id=overrides.pop("result_id", "a" * 32),
        result=result,
        extracted=result.structured_fields,
        fields=result.structured_fields,
        **overrides,
    )


@pytest.mark.parametrize(
    ("values", "default", "expected"),
    [
        (None, True, True),
        ([], True, True),
        (["0", "1"], False, True),
        (["0"], True, False),
        (["on"], False, True),
        (["true"], False, True),
        (["no", "off"], True, False),
    ],
)
def test_checkbox_value_reads_the_hidden_checkbox_pair(values, default, expected):
    assert checkbox_value(values, default) is expected


def test_a_document_reports_what_the_reviewer_changed():
    document = review_document()

    assert document.is_corrected is False
    assert document.corrected_keys == ()

    corrected = ReviewDocument(
        result_id=document.result_id,
        result=document.result,
        extracted=document.extracted,
        fields=validate_fields(
            {FIELD_SUPPLIER: "Acme GmbH", FIELD_TOTAL_AMOUNT: "140.42"},
            base=document.extracted,
        )[0],
    )

    assert corrected.is_corrected is True
    assert corrected.corrected_keys == (FIELD_SUPPLIER, FIELD_TOTAL_AMOUNT), (
        "only the values that actually changed are reported"
    )
    assert corrected.error_for(FIELD_TOTAL_AMOUNT) is None



def test_batch_counts_what_will_be_stored():
    batch = ReviewBatch(
        documents=(
            review_document(result_id="a" * 32),
            review_document(result_id="b" * 32, included=False),
        )
    )

    assert len(batch) == 2 and batch.count == 2
    assert batch.is_single is False
    assert [document.result_id for document in batch.selected] == ["a" * 32]
    assert [document.result_id for document in batch.skipped] == ["b" * 32]
    assert batch.is_valid is True
    assert batch.saved_count == 0


def test_documents_from_prefills_the_extracted_fields():
    documents = documents_from([("a" * 32, extraction()), ("b" * 32, extraction())])

    assert [document.result_id for document in documents] == ["a" * 32, "b" * 32]
    assert all(document.fields.value("invoice_number") == "10042" for document in documents)
    assert all(document.fields is document.extracted for document in documents)


def test_submitted_result_ids_follow_the_form_order():
    form = MultiDict([("result_id", "a" * 32), ("supplier_0", "x"), ("result_id", "b" * 32)])

    assert submitted_result_ids(form) == ("a" * 32, "b" * 32)
    assert submitted_result_ids({"result_id": "c" * 32}) == ("c" * 32,)
    assert submitted_result_ids({}) == ()


def test_submitted_field_values_only_include_inputs_the_form_carried():
    form = MultiDict([("result_id", "a" * 32), (f"{FIELD_SUPPLIER}_0", "Acme GmbH")])

    values = submitted_field_values(form, 0)

    assert values == {FIELD_SUPPLIER: "Acme GmbH"}
    assert FIELD_TOTAL_AMOUNT not in values, "an absent input is not a cleared field"


def test_parse_form_applies_corrections_and_reports_bad_values():
    documents = (review_document(), review_document(result_id="b" * 32))
    form = MultiDict(
        [
            ("result_id", "a" * 32),
            ("result_id", "b" * 32),
            ("include_0", "0"),
            ("include_0", "1"),
            (f"{FIELD_TOTAL_AMOUNT}_0", "1.234,56"),
            (f"{FIELD_TOTAL_AMOUNT}_1", "???"),
        ]
    )

    reviewed, notices = parse_form(form, documents)

    assert reviewed[0].fields.value(FIELD_TOTAL_AMOUNT) == "1234.56"
    assert reviewed[0].included is True
    assert reviewed[0].is_corrected is True
    assert reviewed[0].has_field_errors is False

    assert reviewed[1].has_field_errors is True
    assert reviewed[1].field_errors[0][0] == FIELD_TOTAL_AMOUNT
    assert "Use a number" in reviewed[1].error_for(FIELD_TOTAL_AMOUNT)
    assert reviewed[1].fields.value(FIELD_TOTAL_AMOUNT) == "128.50", "nothing is lost"
    assert notices and "invoice.pdf" in notices[0]


def test_parse_form_unticked_document_is_kept_but_not_selected():
    form = MultiDict([("result_id", "a" * 32), ("include_0", "0")])

    reviewed, _ = parse_form(form, (review_document(),))

    assert reviewed[0].included is False
    assert reviewed[0].is_saved is False


# ---------------------------------------------------------------------------
# the flow: upload -> review -> save (real SQLite file, text-layer PDFs)
# ---------------------------------------------------------------------------
def upload_documents(client, *files, url: str = "/upload", **form):
    """``POST <url>`` with one multipart ``file`` part per document."""
    data: dict = dict(form)
    data["file"] = [(io.BytesIO(payload), name) for name, payload in files]
    return client.post(url, data=data, content_type="multipart/form-data")



def test_uploading_renders_the_review_page_and_stores_nothing(
    sqlite_client, sqlite_store, text_pdf_factory
):
    response = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT))
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "nothing has been stored yet" in body, "the review step comes first"
    assert "Save the reviewed data to SQLite" in body
    assert 'value="10042"' in body, "the extracted fields are pre-filled"
    assert 'value="128.50"' in body
    assert 'name="result_id"' in body and "Open result page" in body
    assert "Extracted text" in body
    assert sqlite_store.record_count() == 0


def test_a_batch_of_files_is_reviewed_on_one_page(
    sqlite_client, sqlite_store, text_pdf_factory
):
    response = upload_documents(
        sqlite_client,
        ("first.pdf", text_pdf_factory("Acme invoice 2026\nTotal: 10.00 EUR")),
        ("second.pdf", text_pdf_factory("Beta invoice 2026\nTotal: 20.00 EUR")),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body.count('name="result_id"') == 2
    assert "first.pdf" in body and "second.pdf" in body
    assert "Check the extracted data of 2 documents" in body
    assert 'value="10.00"' in body and 'value="20.00"' in body

    saved = sqlite_client.post(
        "/review/save",
        data={
            "result_id": RESULT_ID.findall(body),
            "supplier_0": "Acme GmbH",
            "supplier_1": "Beta AG",
            "save_to_db": ["0", "1"],
        },
    )

    assert saved.status_code == 200
    assert "Stored in SQLite as" in saved.get_data(as_text=True)
    assert sqlite_store.record_count() == 2
    rows = sqlite_store.search_extractions("", 10)
    assert {row["supplier"] for row in rows} == {"Acme GmbH", "Beta AG"}


def test_the_reviewer_can_drop_one_document_of_a_batch(
    sqlite_client, sqlite_store, text_pdf_factory
):
    body = upload_documents(
        sqlite_client,
        ("keep.pdf", text_pdf_factory("Keep invoice 2026\nTotal: 1.00 EUR")),
        ("drop.pdf", text_pdf_factory("Drop invoice 2026\nTotal: 2.00 EUR")),
    ).get_data(as_text=True)

    saved = sqlite_client.post(
        "/review/save",
        data={
            "result_id": RESULT_ID.findall(body),
            "include_0": ["0", "1"],
            "include_1": "0",
            "save_to_db": ["0", "1"],
        },
    )
    page = saved.get_data(as_text=True)

    assert saved.status_code == 200
    assert sqlite_store.record_count() == 1
    assert "Left out of this submit - nothing stored." in page
    assert [row["filename"] for row in sqlite_store.search_extractions("", 10)] == ["keep.pdf"]


def test_an_unreadable_value_comes_back_with_the_message_and_stores_nothing(
    sqlite_client, sqlite_store, text_pdf_factory
):
    body = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT))
    ).get_data(as_text=True)

    rejected = sqlite_client.post(
        "/review/save",
        data={
            "result_id": RESULT_ID.findall(body),
            "total_amount_0": "not a number",
            "save_to_db": ["0", "1"],
        },
    )
    page = rejected.get_data(as_text=True)

    assert rejected.status_code == 400
    assert "use a number" in page.lower()
    assert "not a number" in page, "the message quotes what was rejected"
    assert "is-invalid" in page, "the field is highlighted"
    assert sqlite_store.record_count() == 0, "a rejected review stores nothing"
    assert f'value="{INVOICE_TEXT.splitlines()[-1].split()[-2]}"' in page, (
        "the field keeps the extracted value so the reviewer can retry"
    )


def test_reviewing_a_cached_result_again_is_possible(
    sqlite_client, sqlite_store, text_pdf_factory
):
    body = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT))
    ).get_data(as_text=True)
    result_id = RESULT_ID.findall(body)[0]

    again = sqlite_client.get(f"/review/{result_id}")

    assert again.status_code == 200
    assert 'value="10042"' in again.get_data(as_text=True)
    assert sqlite_store.record_count() == 0

    assert sqlite_client.get("/review/" + "0" * 32).status_code == 404


def test_review_save_without_a_result_id_is_rejected(sqlite_client):
    response = sqlite_client.post("/review/save", data={"supplier_0": "Acme"})

    assert response.status_code == 400
    assert "did not name any document" in response.get_data(as_text=True)


def test_a_vanished_result_cannot_be_stored(
    sqlite_app, sqlite_client, text_pdf_factory
):
    """Between the review page and the submit the cache may have dropped a result."""
    body = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT))
    ).get_data(as_text=True)
    sqlite_app.extensions["ocr_result_store"].clear()

    response = sqlite_client.post("/review/save", data={"result_id": RESULT_ID.findall(body)})

    assert response.status_code == 404
    assert "no longer available" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# batch limits and upload validation
# ---------------------------------------------------------------------------
def test_too_many_files_in_one_batch_are_refused(make_client, tmp_path, text_pdf_factory):
    client = make_client(
        DATABASE_BACKEND="sqlite",
        SQLITE_PATH=str(tmp_path / "x.sqlite3"),
        MAX_BATCH_FILES=2,
    )

    response = upload_documents(
        client,
        ("a.pdf", text_pdf_factory("A invoice 2026")),
        ("b.pdf", text_pdf_factory("B invoice 2026")),
        ("c.pdf", text_pdf_factory("C invoice 2026")),
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert "3 files were submitted" in body and "at most 2" in body
    assert "MAX_BATCH_FILES" in body, "the message says how to raise the cap"


def test_one_bad_file_rejects_the_whole_batch(make_client, tmp_path, text_pdf_factory):
    client = make_client(DATABASE_BACKEND="sqlite", SQLITE_PATH=str(tmp_path / "x.sqlite3"))

    response = upload_documents(
        client,
        ("good.pdf", text_pdf_factory("Good invoice 2026")),
        ("bad.txt", b"just text"),
    )

    assert response.status_code == 400, "nothing is OCR'd until every file is acceptable"
    assert "is not supported" in response.get_data(as_text=True)


def test_the_batch_endpoint_answers_one_entry_per_document(
    sqlite_client, sqlite_store, text_pdf_factory
):
    response = upload_documents(
        sqlite_client,
        ("one.pdf", text_pdf_factory("One invoice 2026\nTotal: 1.00 EUR")),
        ("two.pdf", text_pdf_factory("Two invoice 2026\nTotal: 2.00 EUR")),
        url="/api/ocr/batch",
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True and payload["count"] == 2 and payload["saved"] == 2
    assert [entry["filename"] for entry in payload["results"]] == ["one.pdf", "two.pdf"]
    assert payload["results"][0]["fields"]["total_amount"] == "1.00"
    assert payload["results"][0]["database"]["record_id"] == 1
    assert sqlite_store.record_count() == 2


def test_api_ocr_refuses_a_batch_and_points_at_the_batch_endpoint(
    sqlite_client, text_pdf_factory
):
    response = upload_documents(
        sqlite_client,
        ("one.pdf", text_pdf_factory("One invoice 2026")),
        ("two.pdf", text_pdf_factory("Two invoice 2026")),
        url="/api/ocr",
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "too_many_files"
    assert "/api/ocr/batch" in response.get_json()["error"]["message"]


def test_api_ocr_reports_the_extracted_fields_and_never_leaves_the_page_unnamed(
    sqlite_client, sqlite_store, text_pdf_factory
):
    response = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    )
    payload = response.get_json()

    assert payload["fields"] == {
        "supplier": "ACME",
        "invoice_number": "10042",
        "document_date": None,
        "total_amount": "128.50",
        "currency": "EUR",
    }
    assert payload["field_confidence"]["invoice_number"] == 90.0
    assert payload["review_url"].startswith("/review/")
    assert payload["database"]["saved"] is True, "the API has no review step"
    assert sqlite_store.record_count() == 1


# ---------------------------------------------------------------------------
# the JSON review endpoint
# ---------------------------------------------------------------------------
def test_api_review_save_stores_corrected_fields(sqlite_client, sqlite_store, text_pdf_factory):
    upload = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    ).get_json()

    response = sqlite_client.post(
        "/api/review/save",
        json={
            "documents": [
                {
                    "result_id": upload["result_id"],
                    "fields": {"supplier": "Acme GmbH", "document_date": "15.03.2026"},
                    "save": True,
                }
            ]
        },
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["saved"] == 1 and payload["count"] == 1
    document = payload["documents"][0]
    assert document["fields"]["supplier"] == "Acme GmbH"
    assert document["fields"]["document_date"] == "2026-03-15"
    assert document["fields"]["total_amount"] == "128.50", "untouched values are kept"
    assert document["corrected"] == ["supplier", "document_date"]
    assert sqlite_store.record_count() == 2, "the API upload was stored as well"

    stored = sqlite_store.get_extraction(document["record_id"])
    assert stored["supplier"] == "Acme GmbH"
    assert stored["document_date"] == "2026-03-15"


def test_api_review_save_accepts_a_single_flat_document(sqlite_client, sqlite_store, text_pdf_factory):
    upload = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    ).get_json()

    response = sqlite_client.post(
        "/api/review/save",
        json={"result_id": upload["result_id"], "fields": {"currency": "usd"}},
    )

    assert response.status_code == 200
    assert response.get_json()["documents"][0]["fields"]["currency"] == "USD"


def test_api_review_save_rejects_an_unreadable_value(sqlite_client, sqlite_store, text_pdf_factory):
    upload = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    ).get_json()
    before = sqlite_store.record_count()

    response = sqlite_client.post(
        "/api/review/save",
        json={"result_id": upload["result_id"], "fields": {"total_amount": "nope"}},
    )
    payload = response.get_json()

    assert response.status_code == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_field_value"
    assert "Use a number" in payload["fields"][upload["result_id"]]["total_amount"]
    assert sqlite_store.record_count() == before, "nothing was stored"


def test_api_review_save_can_be_told_not_to_store(sqlite_client, sqlite_store, text_pdf_factory):
    upload = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    ).get_json()

    response = sqlite_client.post(
        "/api/review/save",
        json={"result_id": upload["result_id"], "fields": {"supplier": "Acme GmbH"}, "save": False},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["saved"] == 0
    assert payload["documents"][0]["saved"] is False
    assert payload["documents"][0]["fields"]["supplier"] == "Acme GmbH"


def test_api_review_save_reports_an_expired_result(sqlite_app, sqlite_client, text_pdf_factory):
    upload = upload_documents(
        sqlite_client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT)), url="/api/ocr"
    ).get_json()
    sqlite_app.extensions["ocr_result_store"].clear()

    response = sqlite_client.post(
        "/api/review/save", json={"result_id": upload["result_id"], "fields": {}}
    )

    assert response.status_code == 400
    assert "no longer available" in response.get_json()["fields"][upload["result_id"]]["result_id"][0]


def test_api_review_save_needs_a_document(sqlite_client):
    assert sqlite_client.post("/api/review/save", json={}).status_code == 400
    assert sqlite_client.post("/api/review/save", json={"documents": []}).status_code == 400
    assert sqlite_client.post("/api/review/save", json={"documents": [{}]}).status_code == 400
    assert sqlite_client.post(
        "/api/review/save", data="not json", content_type="text/plain"
    ).status_code == 400


# ---------------------------------------------------------------------------
# a store created before the structured fields existed
# ---------------------------------------------------------------------------
#: The ``ocr_extractions`` table exactly as version 1 of this project created it -
#: no supplier, no invoice number, no date, no total, no currency.
LEGACY_TABLE_SQL = """
CREATE TABLE `ocr_extractions` (
  `id` INTEGER PRIMARY KEY AUTOINCREMENT,
  `filename` VARCHAR(255) NOT NULL,
  `uploaded_at` DATETIME NOT NULL,
  `content` TEXT NOT NULL,
  `kind` VARCHAR(16) NOT NULL DEFAULT 'image',
  `page_count` INTEGER NOT NULL DEFAULT 0,
  `char_count` INTEGER NOT NULL DEFAULT 0,
  `word_count` INTEGER NOT NULL DEFAULT 0,
  `confidence` REAL NULL,
  `duration_ms` INTEGER NOT NULL DEFAULT 0,
  `size_bytes` INTEGER NOT NULL DEFAULT 0,
  `ocr_language` VARCHAR(64) NULL,
  `engine_version` VARCHAR(64) NULL,
  `content_sha256` CHAR(64) NULL,
  `stored_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def legacy_store(path, *, rows: int = 1) -> None:
    """Write that old table (with a row) to *path*."""
    with sqlite3.connect(str(path)) as connection:
        connection.executescript(LEGACY_TABLE_SQL)
        for index in range(rows):
            connection.execute(
                "INSERT INTO `ocr_extractions` "
                "(`filename`, `uploaded_at`, `content`, `kind`, `page_count`, "
                " `char_count`, `word_count`, `duration_ms`, `size_bytes`, "
                " `ocr_language`, `engine_version`, `content_sha256`) "
                "VALUES (?, '2026-01-01 00:00:00', 'old document', 'pdf', 1, 12, 2, 5, 10, "
                "        'eng', '5.4.0', NULL)",
                (f"legacy-{index}.pdf",),
            )
        connection.commit()


def test_connecting_upgrades_a_table_without_the_structured_fields(
    make_client, tmp_path, text_pdf_factory
):
    """An older database must keep working - its rows stay, the columns appear."""
    path = tmp_path / "legacy.sqlite3"
    legacy_store(path, rows=2)

    client = make_client(DATABASE_BACKEND="sqlite", SQLITE_PATH=str(path))
    client.post("/database/connect", data={"backend": "sqlite", "path": str(path)})

    with sqlite3.connect(str(path)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(`ocr_extractions`)")
        }
        stored = connection.execute("SELECT COUNT(*) FROM `ocr_extractions`").fetchone()[0]

    assert {"supplier", "invoice_number", "document_date", "total_amount", "currency"} <= columns
    assert stored == 2, "the migration adds columns, it never touches a row"

    # ... and saving into the upgraded table works, fields included.
    body = upload_documents(
        client, ("invoice.pdf", text_pdf_factory(INVOICE_TEXT))
    ).get_data(as_text=True)
    saved = client.post(
        "/review/save",
        data={"result_id": RESULT_ID.findall(body), "save_to_db": ["0", "1"]},
    )

    assert saved.status_code == 200
    with sqlite3.connect(str(path)) as connection:
        row = connection.execute(
            "SELECT `supplier`, `invoice_number`, `total_amount`, `currency` "
            "FROM `ocr_extractions` WHERE `filename` = 'invoice.pdf'"
        ).fetchone()
    assert row == ("ACME", "10042", 128.5, "EUR")


def test_the_schema_endpoint_reports_nothing_to_do_on_a_current_store(
    sqlite_client, sqlite_store
):
    """The upgrade is idempotent: a second connect must not try to add anything."""
    assert sqlite_store._add_field_columns() == ()
    schema = sqlite_client.post("/database/schema")

    assert schema.status_code in (200, 303)






