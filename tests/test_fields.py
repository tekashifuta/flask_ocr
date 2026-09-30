"""Unit tests for the structured field extraction (``app/fields.py``).

Nothing here touches OCR, Tesseract or a database: the module turns text into
values, so the text is all these tests need - including the very texts the sample
files produce, so a change in the rules is immediately visible on the documents the
submission ships.
"""

from __future__ import annotations

import pytest

from app.fields import (
    CONFIDENCE_GUESSED,
    CONFIDENCE_LABELLED,
    FIELD_CURRENCY,
    FIELD_DOCUMENT_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_ORDER,
    FIELD_SUPPLIER,
    FIELD_TOTAL_AMOUNT,
    DocumentFields,
    FieldValue,
    clean_identifier,
    extract_fields,
    looks_like_invoice,
    normalize_currency,
    normalize_date,
    parse_amount,
    validate_field,
    validate_fields,
)

#: Exactly what the application extracted from ``samples/images/scan_invoice.png``.
INVOICE_TEXT = "ACME invoice 2026\nInvoice no: 10042\nTotal: 128.50 EUR"
#: ... and from ``samples/images/scan_receipt.jpg``.
RECEIPT_TEXT = "Corner Coffee\nLatte 3.50 EUR\nThanks for your visit"


def test_the_sample_invoice_yields_number_total_and_supplier():
    fields = extract_fields(INVOICE_TEXT, filename="scan_invoice.png")

    assert fields.value(FIELD_INVOICE_NUMBER) == "10042"
    assert fields.value(FIELD_TOTAL_AMOUNT) == "128.50"
    assert fields.value(FIELD_CURRENCY) == "EUR"
    assert fields.value(FIELD_SUPPLIER) == "ACME"
    assert fields.get(FIELD_INVOICE_NUMBER).confidence == CONFIDENCE_LABELLED
    assert fields.get(FIELD_SUPPLIER).confidence == CONFIDENCE_GUESSED, (
        "a header line is a guess, not a labelled value"
    )
    assert fields.get(FIELD_INVOICE_NUMBER).source_line == "Invoice no: 10042"
    assert fields.value(FIELD_DOCUMENT_DATE) is None, "that sample has no date"


def test_the_sample_receipt_yields_its_three_lines():
    fields = extract_fields(RECEIPT_TEXT, filename="scan_receipt.jpg")

    assert fields.value(FIELD_SUPPLIER) == "Corner Coffee"
    assert fields.value(FIELD_TOTAL_AMOUNT) == "3.50", "the only amount is the total"
    assert fields.value(FIELD_CURRENCY) == "EUR"


def test_a_labelled_invoice_is_read_field_by_field():
    fields = extract_fields(
        "NORTHWIND TRADING GMBH\n"
        "Supplier: Northwind Trading GmbH\n"
        "Invoice no: INV-2026-0042\n"
        "Date: 15.03.2026\n"
        "Subtotal: 118.00 EUR\n"
        "VAT 19%: 22.42 EUR\n"
        "Total: 140.42 EUR",
        filename="scan_invoice_fields.png",
    )

    assert fields.to_dict() == {
        FIELD_SUPPLIER: "Northwind Trading GmbH",
        FIELD_INVOICE_NUMBER: "INV-2026-0042",
        FIELD_DOCUMENT_DATE: "2026-03-15",
        FIELD_TOTAL_AMOUNT: "140.42",
        FIELD_CURRENCY: "EUR",
    }
    assert fields.filled_count == len(FIELD_ORDER)
    assert all(value.confidence == CONFIDENCE_LABELLED for value in fields)


def test_a_document_letter_invents_nothing():
    """A plain report has no invoice labels - so no supplier and no number."""
    fields = extract_fields(
        "Quarterly report Q1 2026\nRevenue 1,240,000 EUR\nPrepared by the finance team."
    )

    assert fields.value(FIELD_SUPPLIER) is None
    assert fields.value(FIELD_INVOICE_NUMBER) is None
    assert fields.value(FIELD_TOTAL_AMOUNT) == "1240000.00", "the largest amount is a guess"
    assert fields.get(FIELD_TOTAL_AMOUNT).confidence == CONFIDENCE_GUESSED


def test_an_empty_document_has_empty_fields():
    fields = extract_fields("", filename="blank_page.png")

    assert fields.is_empty
    assert fields.filled_count == 0
    assert fields.summary == ""
    assert [value.key for value in fields] == list(FIELD_ORDER)


def test_looks_like_invoice_uses_the_file_name_as_a_hint():
    assert looks_like_invoice("nothing to see", "scan_receipt.jpg") is True
    assert looks_like_invoice("nothing to see", "notes.pdf") is False
    assert looks_like_invoice("Total: 1.00") is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("15.03.2026", "2026-03-15"),
        ("2026-03-15", "2026-03-15"),
        ("3/15/2026", "2026-03-15"),
        ("15/03/2026", "2026-03-15"),
        ("15 March 2026", "2026-03-15"),
        ("March 15, 2026", "2026-03-15"),
        ("15.03.26", "2026-03-15"),
        ("Date: 1.2.2026", "2026-02-01"),
        ("2026-13-45", None),
        ("not a date", None),
        ("", None),
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("128.50", "128.50"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1,240,000", "1240000.00"),
        ("1.240.000,00", "1240000.00"),
        ("€ 128,50", "128.50"),
        ("128.50 EUR", "128.50"),
        ("1.234", "1234.00"),
        ("12.345", "12345.00"),
        ("-12.50", "-12.50"),
        ("0", "0.00"),
        ("9999999999.99", "9999999999.99"),
        ("99999999999", None),
        ("total", None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("EUR", "EUR"), ("eur", "EUR"), ("€", "EUR"), ("$", "USD"), ("Euros", None), ("", None)],
)
def test_normalize_currency(raw, expected):
    assert normalize_currency(raw) == expected


def test_clean_identifier_keeps_digits_and_tightens_separators():
    assert clean_identifier("INV - 2026 - 0042") == "INV-2026-0042"
    assert clean_identifier("10042.") == "10042"
    assert clean_identifier("see below") is None, "a number has digits"
    assert len(clean_identifier("1" * 100)) == 64


def test_fields_are_normalised_into_field_order():
    fields = DocumentFields(
        (FieldValue(FIELD_TOTAL_AMOUNT, "1.00"), FieldValue(FIELD_SUPPLIER, "Acme"))
    )

    assert [value.key for value in fields] == list(FIELD_ORDER)
    assert fields.value(FIELD_SUPPLIER) == "Acme"
    assert fields.value(FIELD_INVOICE_NUMBER) is None
    assert fields.get(FIELD_CURRENCY).quality == "missing"
    assert fields.summary == "Acme · 1.00"
    assert fields.to_row() == {
        FIELD_SUPPLIER: "Acme",
        FIELD_INVOICE_NUMBER: None,
        FIELD_DOCUMENT_DATE: None,
        FIELD_TOTAL_AMOUNT: "1.00",
        FIELD_CURRENCY: None,
    }


def test_quality_labels_how_a_value_was_found():
    assert FieldValue(FIELD_SUPPLIER).quality == "missing"
    assert FieldValue(FIELD_SUPPLIER, "Acme", CONFIDENCE_LABELLED).quality == "high"
    assert FieldValue(FIELD_SUPPLIER, "Acme", CONFIDENCE_GUESSED).quality == "low"
    assert FieldValue(FIELD_SUPPLIER, "Acme").quality == "reviewed"


def test_validate_field_accepts_what_the_reviewer_means():
    assert validate_field(FIELD_TOTAL_AMOUNT, "1.234,56") == ("1234.56", None)
    assert validate_field(FIELD_DOCUMENT_DATE, "15.03.2026") == ("2026-03-15", None)
    assert validate_field(FIELD_CURRENCY, "eur") == ("EUR", None)
    assert validate_field(FIELD_SUPPLIER, "  Acme   GmbH ") == ("Acme GmbH", None)
    assert validate_field(FIELD_SUPPLIER, "   ") == (None, None), "blank clears the field"
    assert validate_field(FIELD_SUPPLIER, None) == (None, None)


def test_validate_field_rejects_and_quotes_what_it_could_not_read():
    value, error = validate_field(FIELD_TOTAL_AMOUNT, "not a number")

    assert value is None
    assert "not a number" in error and "Use a number" in error

    value, error = validate_field(FIELD_DOCUMENT_DATE, "whenever")
    assert value is None
    assert "Use a date" in error

    value, error = validate_field(FIELD_CURRENCY, "Euro")
    assert value is None
    assert "three letter code" in error

    value, error = validate_field(FIELD_INVOICE_NUMBER, "no digits here")
    assert value is None
    assert "letters, digits" in error


def test_validate_fields_keeps_what_it_cannot_replace():
    base = extract_fields(INVOICE_TEXT, filename="scan_invoice.png")

    fields, errors = validate_fields(
        {FIELD_SUPPLIER: "Acme GmbH", FIELD_TOTAL_AMOUNT: "???"}, base=base
    )

    assert "Use a number" in errors[FIELD_TOTAL_AMOUNT]
    assert fields.value(FIELD_SUPPLIER) == "Acme GmbH", "the correction is applied"
    assert fields.value(FIELD_TOTAL_AMOUNT) == "128.50", "the unusable value replaces nothing"
    assert fields.value(FIELD_INVOICE_NUMBER) == "10042", "absent keys keep the extracted value"

