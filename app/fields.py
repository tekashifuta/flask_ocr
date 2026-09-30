"""Structured fields pulled out of the extracted text.

The OCR pipeline produces one long string per document.  Bookkeeping needs more
than that: the supplier, the invoice number, the document date and the total
amount, each as a value that fits in its own database column so it can be
searched, corrected and exported to a spreadsheet.

Nothing here is a black box - every field is found by a small readable rule and
carries a **confidence** (0-100) that says *how* it was found:

``90``
    An explicit label in the document (``Invoice no: 10042``, ``Total: 128.50 EUR``).
``60``
    No label, but the value is the only plausible candidate (the first date).
``45``
    A guess (the first line as the supplier, the largest amount as the total).

Those are heuristics, never certainties, so the values are **proposals**: the
review page renders them pre-filled next to the extracted text and a human
confirms or corrects them *before* anything is stored (:mod:`app.review`).  The
review form also validates what was typed - :func:`validate_field` is the same
function behind both that form and the JSON API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

# --- the fields ------------------------------------------------------------
FIELD_SUPPLIER = "supplier"
FIELD_INVOICE_NUMBER = "invoice_number"
FIELD_DOCUMENT_DATE = "document_date"
FIELD_TOTAL_AMOUNT = "total_amount"
FIELD_CURRENCY = "currency"

#: The order every page, form and spreadsheet uses - and the key order of
#: :meth:`DocumentFields.to_row`, which the storage layer binds positionally.
FIELD_ORDER: tuple[str, ...] = (
    FIELD_SUPPLIER,
    FIELD_INVOICE_NUMBER,
    FIELD_DOCUMENT_DATE,
    FIELD_TOTAL_AMOUNT,
    FIELD_CURRENCY,
)

#: Column headings for the UI and the ``.xlsx`` export.
FIELD_LABELS: dict[str, str] = {
    FIELD_SUPPLIER: "Supplier",
    FIELD_INVOICE_NUMBER: "Invoice number",
    FIELD_DOCUMENT_DATE: "Date",
    FIELD_TOTAL_AMOUNT: "Total amount",
    FIELD_CURRENCY: "Currency",
}

#: What kind of value each field holds - drives both parsing and validation.
FIELD_KINDS: dict[str, str] = {
    FIELD_SUPPLIER: "text",
    FIELD_INVOICE_NUMBER: "text",
    FIELD_DOCUMENT_DATE: "date",
    FIELD_TOTAL_AMOUNT: "amount",
    FIELD_CURRENCY: "currency",
}

#: Widest value each field accepts, matching the database columns: a supplier
#: name is ``VARCHAR(255)``, an identifier ``VARCHAR(64)``, a code ``CHAR(3)``.
FIELD_MAX_CHARS: dict[str, int] = {
    FIELD_SUPPLIER: 255,
    FIELD_INVOICE_NUMBER: 64,
    FIELD_CURRENCY: 3,
}

#: Example values, shown as placeholders on the review page.
FIELD_EXAMPLES: dict[str, str] = {
    FIELD_SUPPLIER: "Acme GmbH",
    FIELD_INVOICE_NUMBER: "INV-2026-0042",
    FIELD_DOCUMENT_DATE: "2026-03-15",
    FIELD_TOTAL_AMOUNT: "128.50",
    FIELD_CURRENCY: "EUR",
}

#: How the reviewer should write each value (the ``title`` of the input).
FIELD_HINTS: dict[str, str] = {
    FIELD_SUPPLIER: "The company that issued the document.",
    FIELD_INVOICE_NUMBER: "Letters, digits and - / . only.",
    FIELD_DOCUMENT_DATE: "Any usual format - stored as YYYY-MM-DD.",
    FIELD_TOTAL_AMOUNT: "The gross total; 1.234,56 and 1,234.56 are both understood.",
    FIELD_CURRENCY: "Three letter code, e.g. EUR or USD.",
}

# --- confidence ------------------------------------------------------------
CONFIDENCE_LABELLED = 90.0
#: Found without a label, but unambiguous (the only date on the page).
CONFIDENCE_OBVIOUS = 60.0
#: A heuristic guess - shown to be corrected.
CONFIDENCE_GUESSED = 45.0
#: Above this the badge reads "high"; below it "low" (see ``FieldValue.quality``).
HIGH_CONFIDENCE = 80.0
MEDIUM_CONFIDENCE = 55.0


@dataclass(frozen=True)
class FieldValue:
    """One structured field: its value (or ``None``) and how it was obtained."""

    key: str
    value: str | None = None
    confidence: float | None = None
    #: The line the value came from - shown as "found in" on the review page.
    source_line: str | None = None

    @property
    def label(self) -> str:
        return FIELD_LABELS.get(self.key, self.key)

    @property
    def kind(self) -> str:
        return FIELD_KINDS.get(self.key, "text")

    @property
    def max_chars(self) -> int | None:
        return FIELD_MAX_CHARS.get(self.key)

    @property
    def example(self) -> str:
        return FIELD_EXAMPLES.get(self.key, "")

    @property
    def hint(self) -> str:
        return FIELD_HINTS.get(self.key, "")

    @property
    def is_found(self) -> bool:
        """True when the parser proposed a value (what the review form shows)."""
        return bool(self.value)

    @property
    def quality(self) -> str:
        """``high`` / ``medium`` / ``low`` / ``reviewed`` / ``missing`` - the UI badge."""
        if not self.value:
            return "missing"
        if self.confidence is None:
            # A value a human stands behind (the review form, the JSON API).
            return "reviewed"
        if self.confidence >= HIGH_CONFIDENCE:
            return "high"
        if self.confidence >= MEDIUM_CONFIDENCE:
            return "medium"
        return "low"


    def to_dict(self) -> dict[str, Any]:
        """JSON friendly form, used by ``/api/ocr`` and the review page."""
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "confidence": self.confidence,
            "quality": self.quality,
            "source_line": self.source_line,
        }


def _clean_value(value: object) -> str | None:
    """A stripped string, or ``None`` for anything blank/non-scalar."""
    if value is None or isinstance(value, (bytes, bytearray)):
        return None
    text = str(value).replace("\u00a0", " ").strip()
    return text or None


@dataclass(frozen=True)
class DocumentFields:
    """The structured fields of one document, always all of :data:`FIELD_ORDER`.

    A frozen tuple of :class:`FieldValue` (rather than five attributes) is what
    keeps the review form, the JSON API, the database row and the spreadsheet
    iterating over *the same* keys in *the same* order.
    """

    values: tuple[FieldValue, ...] = ()

    def __post_init__(self) -> None:
        """Guarantee one entry per field, in :data:`FIELD_ORDER`."""
        ordered = tuple(
            next(
                (value for value in self.values if value.key == key),
                FieldValue(key=key),
            )
            for key in FIELD_ORDER
        )
        if ordered != tuple(self.values):
            object.__setattr__(self, "values", ordered)

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def get(self, key: str) -> FieldValue:
        """The :class:`FieldValue` for *key* (an empty one when unknown)."""
        for value in self.values:
            if value.key == key:
                return value
        return FieldValue(key=key)

    def value(self, key: str) -> str | None:
        """Just the string value - what a template prints."""
        return self.get(key).value

    @property
    def filled(self) -> tuple[FieldValue, ...]:
        """The fields the parser actually found something for."""
        return tuple(value for value in self.values if value.value)

    @property
    def missing(self) -> tuple[FieldValue, ...]:
        """The fields that are still empty - what a reviewer has to fill in."""
        return tuple(value for value in self.values if not value.value)

    @property
    def filled_count(self) -> int:
        return len(self.filled)

    @property
    def is_empty(self) -> bool:
        return not self.filled

    @property
    def summary(self) -> str:
        """One line for the records table, e.g. ``Acme - INV-7 - 128.50 EUR``."""
        money = " ".join(
            part
            for part in (self.value(FIELD_TOTAL_AMOUNT), self.value(FIELD_CURRENCY))
            if part
        )
        parts = (
            self.value(FIELD_SUPPLIER),
            self.value(FIELD_INVOICE_NUMBER),
            self.value(FIELD_DOCUMENT_DATE),
            money or None,
        )
        return " · ".join(part for part in parts if part)

    def to_dict(self) -> dict[str, str | None]:
        """``{"supplier": "Acme", ...}`` - the plain values, for storage/JSON."""
        return {value.key: value.value for value in self.values}

    def to_row(self) -> dict[str, object]:
        """The mapping the storage layer binds: empty values become ``NULL``."""
        return {key: (self.value(key) or None) for key in FIELD_ORDER}

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None = None) -> "DocumentFields":
        """Fields built from explicit values (the review form, an API caller).

        No confidence is attached: these are values a human stands behind, not
        something a rule guessed.
        """
        provided = values or {}
        return cls(
            tuple(
                FieldValue(key=key, value=_clean_value(provided.get(key)))
                for key in FIELD_ORDER
            )
        )

    @classmethod
    def empty(cls) -> "DocumentFields":
        return cls(tuple(FieldValue(key=key) for key in FIELD_ORDER))


# ---------------------------------------------------------------------------
# normalisers - shared by the parser, the review form and the JSON API
# ---------------------------------------------------------------------------
#: Currency symbol -> ISO code, so ``128.50 €`` and ``128.50 EUR`` agree.
CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₣": "CHF",
}

#: What the parser accepts as a currency code.  Anything else a reviewer types is
#: still stored - it is simply never *guessed* from thin air.
KNOWN_CURRENCIES = frozenset(
    {
        "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP",
        "HKD", "HUF", "INR", "ILS", "ISK", "JPY", "KRW", "MXN", "NOK", "NZD",
        "PLN", "RON", "RUB", "SEK", "SGD", "TRY", "UAH", "USD", "ZAR",
    }
)

#: ``DECIMAL(12, 2)`` - what the database column can hold.
MAX_AMOUNT = Decimal("9999999999.99")
_AMOUNT_QUANTUM = Decimal("0.01")

#: Month names the date parser understands - an abbreviation or the full name
#: (English first, then the German/French names the OCR may produce).
_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "maerz": 3, "märz": 3, "mai": 5, "oktober": 10,
    "dezember": 12, "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12,
}


_DATE_ISO_RE = re.compile(
    r"(?<!\d)(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?!\d)"
)
_DATE_NUMERIC_RE = re.compile(
    r"(?<!\d)(?P<first>\d{1,2})[-/.]\s?(?P<second>\d{1,2})[-/.]\s?(?P<year>\d{2,4})(?!\d)"
)
_DATE_DAY_MONTH_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s*\.?\s*(?P<month>[A-Za-z]{3,9})\.?,?\s*(?P<year>\d{2,4})(?!\d)"
)
_DATE_MONTH_DAY_RE = re.compile(
    r"(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?P<year>\d{2,4})(?!\d)"
)


def _as_year(value: str) -> int:
    """``26`` -> ``2026``; four digit years are taken as they are."""
    number = int(value)
    return 2000 + number if number < 100 else number


def _build_date(year: int, month: int, day: int) -> str | None:
    """``date`` validates for us (and rejects e.g. February 31st)."""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _month_number(name: str) -> int | None:
    return _MONTHS.get(name.strip(".").lower())


def normalize_date(raw: object) -> str | None:
    """Turn a printed date into ``YYYY-MM-DD``, or ``None`` when unreadable.

    Handles ``2026-03-15``, ``15.03.2026``, ``3/15/2026``, ``15 March 2026`` and
    ``March 15, 2026``; two digit years are read as 20xx.  ``dd/mm/yyyy`` and
    ``mm/dd/yyyy`` cannot be told apart, so **day first** wins (``03/04/2026`` is
    the 3rd of April) - unless the first number cannot be a day (``03/15/2026``),
    in which case it is read month first, so both conventions land on the right
    date whenever the numbers allow it.
    """
    text = _clean_value(raw)
    if not text:
        return None

    match = _DATE_ISO_RE.search(text)
    if match:
        return _build_date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )

    match = _DATE_NUMERIC_RE.search(text)
    if match:
        first, second = int(match.group("first")), int(match.group("second"))
        year = _as_year(match.group("year"))
        day, month = (first, second) if first > 12 or second <= 12 else (second, first)
        return _build_date(year, month, day)

    match = _DATE_DAY_MONTH_RE.search(text)
    if match:
        month = _month_number(match.group("month"))
        if month:
            return _build_date(
                _as_year(match.group("year")), month, int(match.group("day"))
            )

    match = _DATE_MONTH_DAY_RE.search(text)
    if match:
        month = _month_number(match.group("month"))
        if month:
            return _build_date(
                _as_year(match.group("year")), month, int(match.group("day"))
            )
    return None


def parse_amount(raw: object) -> str | None:
    """Turn a printed amount into ``1234.56`` (two decimals), else ``None``.

    ``1.234,56``, ``1,234.56``, ``128.50 EUR``, ``€ 128,50`` and ``128`` all work.
    With both separators present the **last** one is the decimal separator; with a
    single separator, exactly three digits after it mean "thousands" (``1.234`` is
    1234) and anything else means decimals (``12.34`` is 12.34).  Values beyond the
    column's range are rejected rather than silently truncated.
    """
    text = _clean_value(raw)
    if not text:
        return None

    compact = text.replace(" ", "").replace("\u00a0", "")
    negative = compact.startswith("-") or compact.endswith("-")
    digits = re.sub(r"[^0-9.,]", "", compact.replace("-", ""))
    if not any(character.isdigit() for character in digits):
        return None

    dots = [index for index, character in enumerate(digits) if character == "."]
    commas = [index for index, character in enumerate(digits) if character == ","]
    decimal_at: int | None = None
    if dots and commas:
        # Both separators are present: the last one is the decimal separator.
        decimal_at = max(dots[-1], commas[-1])
    elif dots or commas:
        positions = dots or commas
        if len(positions) == 1 and len(digits) - positions[0] - 1 != 3:
            # A single separator with anything but three digits after it is a
            # decimal point; three digits are a thousands group ("1.234" = 1234).
            decimal_at = positions[0]

    if decimal_at is None:
        whole, fraction = digits, ""
    else:
        whole, fraction = digits[:decimal_at], digits[decimal_at + 1 :]
    whole = re.sub(r"[^0-9]", "", whole).lstrip("0") or "0"
    fraction = re.sub(r"[^0-9]", "", fraction) or "0"

    try:
        amount = Decimal(f"{whole}.{fraction}")
    except InvalidOperation:  # pragma: no cover - guarded by the checks above
        return None
    if negative:
        amount = -amount
    if abs(amount) > MAX_AMOUNT:
        return None
    return str(amount.quantize(_AMOUNT_QUANTUM))



def normalize_currency(raw: object) -> str | None:
    """``€`` / ``eur`` / ``EUR.`` -> ``EUR`` - a three letter code, else ``None``."""
    text = _clean_value(raw)
    if not text:
        return None
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    letters = re.sub(r"[^A-Za-z]", "", text).upper()
    return letters if len(letters) == 3 else None


_WHITESPACE_RE = re.compile(r"\s+")


def clean_text_value(key: str, raw: object) -> str | None:
    """Tidy a free text field: one line, no stray quoting, within its column."""
    text = _clean_value(raw)
    if not text:
        return None
    text = _WHITESPACE_RE.sub(" ", text).strip("\"'` ,;:·-")
    limit = FIELD_MAX_CHARS.get(key)
    if limit:
        text = text[:limit].strip()
    return text or None


def clean_identifier(raw: object) -> str | None:
    """Tidy an invoice number: ``INV - 2026 - 0042`` -> ``INV-2026-0042``.

    A number always contains a digit, which is what keeps a stray word (``Invoice
    no: see below``) from being stored as one.
    """
    text = clean_text_value(FIELD_INVOICE_NUMBER, raw)
    if not text:
        return None
    text = re.sub(r"\s*([./\-–—_])\s*", r"\1", text).strip(".-_ ")
    if not any(character.isdigit() for character in text):
        return None
    return text[: FIELD_MAX_CHARS[FIELD_INVOICE_NUMBER]].strip() or None


# ---------------------------------------------------------------------------
# labels and candidates
# ---------------------------------------------------------------------------
_LINE_SPLIT_RE = re.compile(r"[\r\n]+")


def _lines(text: object) -> list[str]:
    """The non-empty, stripped lines of *text*."""
    return [line.strip() for line in _LINE_SPLIT_RE.split(str(text or "")) if line.strip()]


def _label_re(labels: tuple[str, ...]) -> re.Pattern[str]:
    """``label <separator> value`` - the separator is required on purpose.

    Without it, a line that merely *starts* with "From" or "Store" would hand the
    rest of the sentence to the parser; with it, ``From: Acme`` and ``Supplier -
    Acme`` match while prose does not.
    """
    alternatives = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    return re.compile(
        rf"^(?:{alternatives})\b\.?\s*[:\-–—]\s*(?P<value>\S.*)$", re.IGNORECASE
    )


#: Words that turn a line into the *supplier* line, e.g. ``Supplier: Acme GmbH``.
SUPPLIER_LABELS: tuple[str, ...] = (
    "supplier", "vendor", "seller", "merchant", "store", "shop", "company",
    "issued by", "billed by", "invoiced by", "sold by", "from", "firma",
    "lieferant", "rechnungssteller", "fournisseur",
)
SUPPLIER_LINE_RE = _label_re(SUPPLIER_LABELS)

#: Words that turn a line into the *invoice number* line.
INVOICE_NUMBER_LABELS: tuple[str, ...] = (
    "invoice number", "invoice no", "invoice nr", "invoice #", "invoice-no",
    "invoice", "inv no", "inv nr", "inv #", "inv.", "bill number", "bill no",
    "receipt number", "receipt no", "document number", "document no", "doc no",
    "our reference", "your reference", "reference number", "reference no",
    "reference", "ref no", "order number", "order no", "customer number",
    "customer no", "rechnungsnummer", "rechnungsnr", "belegnummer",
)
INVOICE_NUMBER_LINE_RE = _label_re(INVOICE_NUMBER_LABELS)

#: Words that turn a line into the *date* line.
DATE_LABELS: tuple[str, ...] = (
    "invoice date", "receipt date", "document date", "date of issue", "issue date",
    "date of invoice", "dated", "date", "datum", "rechnungsdatum", "date facture",
)
DATE_LINE_RE = _label_re(DATE_LABELS)

#: Words that turn a line into the *total* line.  ``subtotal``/``net`` are
#: deliberately absent - they are not the amount a reviewer wants.
TOTAL_LABELS: tuple[str, ...] = (
    "grand total", "total amount", "total due", "total to pay", "amount due",
    "amount payable", "balance due", "invoice total", "total (gross)",
    "total incl. vat", "total including vat", "total", "gesamtbetrag",
    "gesamtsumme", "summe", "betrag", "montant total", "net a payer",
)
TOTAL_LINE_RE = _label_re(TOTAL_LABELS)

#: Lines that must never be mistaken for the supplier: another field's label.
_OTHER_LABEL_RE = re.compile(
    r"^(?:" + "|".join(
        re.escape(label)
        for label in sorted(
            {
                *INVOICE_NUMBER_LABELS, *DATE_LABELS, *TOTAL_LABELS,
                "subtotal", "net", "vat", "tax", "shipping", "postage", "discount",
                "item", "qty", "quantity", "description", "unit price", "price",
                "total", "amount", "page",
            },
            key=len,
            reverse=True,
        )
    ) + r")\b",
    re.IGNORECASE,
)

#: Document words that are *not* part of a company name (``Acme invoice 2026``).
_DOCUMENT_WORDS = frozenset(
    {
        "invoice", "receipt", "bill", "statement", "rechnung", "beleg", "facture",
        "quittung", "tax", "purchase", "order", "po", "quotation", "quote",
        "estimate", "delivery", "note", "credit", "debit", "reminder", "receipts",
        "invoices", "copy", "original", "page", "nr", "no",
    }
)

#: Does this look like an invoice/receipt at all?  Only then is a line without a
#: label allowed to become the supplier; a letter must not invent one.
_DOCUMENT_HINT_RE = re.compile(
    r"\b(invoice|receipt|bill|statement|rechnung|beleg|facture|quittung|"
    r"purchase\s+order|quotation|credit\s+note|delivery\s+note|amount\s+due|"
    r"balance\s+due|subtotal|vat|total)\b",
    re.IGNORECASE,
)

#: A date anywhere in the text (used when no line carries a label).
_DATE_ANY_RE = re.compile(
    r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\s?\d{1,2}[-/.]\s?\d{2,4}"
    r"|\d{1,2}\s*\.?\s*[A-Za-z]{3,9}\.?\,?\s*\d{2,4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{2,4})(?!\d)"
)

#: A number with an optional currency marker - the raw material for amounts.  The
#: trailing ``(?!\d)`` is what stops ``2026`` from being read as ``202``.
_AMOUNT_CANDIDATE_RE = re.compile(
    r"(?<![\w.,])(?P<symbol>[$€£¥₣])?\s?"
    r"(?P<number>\d+(?:[ .,]\d{3})*(?:[.,]\d{1,2})?)(?!\d)"
    r"\s?(?P<code>[A-Z]{3})?"
)


#: A token that looks like a document number (``INV-2026-0042``, ``RG 20260415``).
_IDENTIFIER_TOKEN_RE = re.compile(r"(?<![\w-])(?P<value>[A-Z]{2,6}[-\s]?\d{2,}[-\d]*)(?![\w-])")

#: Years are not amounts - "Q1 2026" must not become the total.
_YEAR_LIKE_RE = re.compile(r"^(?:19|20)\d{2}$")

#: ``scan_receipt.jpg`` becomes ``scan receipt.jpg`` before the hint is matched.
_FILENAME_SEPARATOR_RE = re.compile(r"[_\-]+")



def looks_like_invoice(text: str, filename: str = "") -> bool:
    """True when the text (or the file name) suggests an invoice or receipt.

    ``scan_receipt.jpg`` is a hint too, which is why the file name has its ``_``/``-``
    turned into spaces first: ``\\breceipt\\b`` cannot match inside ``scan_receipt``.
    """
    hint = _FILENAME_SEPARATOR_RE.sub(" ", str(filename or ""))
    haystack = f"{hint}\n{text or ''}"
    return bool(_DOCUMENT_HINT_RE.search(haystack))



def _currency_near(symbol: str | None, code: str | None) -> str | None:
    """The currency written next to an amount (``128.50 EUR`` / ``€ 128.50``)."""
    if symbol:
        return CURRENCY_SYMBOLS.get(symbol)
    if code and code.upper() in KNOWN_CURRENCIES:
        return code.upper()
    return None


def _first_amount(
    text: str, *, require_marker: bool = False
) -> tuple[str | None, str | None]:
    """``(amount, currency)`` for the first usable number in *text*.

    With ``require_marker`` only numbers carrying a decimal fraction or a currency
    marker count - that is what keeps an invoice *number* (``10042``) from being
    read as the total when no label was found.
    """
    for match in _AMOUNT_CANDIDATE_RE.finditer(text or ""):
        raw = match.group("number")
        symbol, code = match.group("symbol"), match.group("code")
        fraction = bool(re.search(r"[.,]\d{1,2}$", raw))
        if _YEAR_LIKE_RE.match(raw.replace(" ", "")) and not symbol and not code:
            continue
        if require_marker and not fraction and not symbol and not _currency_near(None, code):
            continue
        amount = parse_amount(raw)
        if amount:
            return amount, _currency_near(symbol, code)
    return None, None


def _labelled(
    lines: list[str], pattern: re.Pattern[str], clean
) -> tuple[str | None, str | None]:
    """``(value, line)`` from the first line whose label yields a usable value."""
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        value = clean((match.group("value") or "").strip())
        if value:
            return value, line
    return None, None


def _common_currency(text: str) -> str | None:
    """The currency the document mentions most often (``EUR`` beats a stray ``$``)."""
    counts: dict[str, int] = {}
    for symbol, code in CURRENCY_SYMBOLS.items():
        found = (text or "").count(symbol)
        if found:
            counts[code] = counts.get(code, 0) + found
    for match in re.finditer(r"\b[A-Z]{3}\b", text or ""):
        code = match.group(0)
        if code in KNOWN_CURRENCIES:
            counts[code] = counts.get(code, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda code: (counts[code], code))


def _clean_supplier_name(line: str | None) -> str | None:
    """``ACME invoice 2026`` -> ``ACME`` - a company name, not the document title."""
    text = clean_text_value(FIELD_SUPPLIER, line)
    if not text:
        return None
    kept: list[str] = []
    for word in text.split():
        if word.strip(".,:;'\"()[]").lower() in _DOCUMENT_WORDS:
            break
        kept.append(word)
    while kept and (
        _YEAR_LIKE_RE.match(kept[-1].strip(".,"))
        or re.fullmatch(r"\d{1,4}[./-]\d{1,4}(?:[./-]\d{2,4})?", kept[-1].strip(".,"))
    ):
        kept.pop()
    name = " ".join(kept).strip(" -–—.,:;|\"")
    return name if re.search(r"[A-Za-z]{2,}", name) else None


def _line_amount(line: str) -> str | None:
    """The first amount in *line* that is not just a year (``ACME 2026`` has none)."""
    for match in _AMOUNT_CANDIDATE_RE.finditer(line or ""):
        raw = match.group("number")
        if _YEAR_LIKE_RE.match(raw.replace(" ", "")):
            continue
        parsed = parse_amount(raw)
        if parsed:
            return parsed
    return None


def _header_line(lines: list[str], *, skip: set[str]) -> str | None:
    """The first line that could be a company name (no label, not another value)."""
    for line in lines:
        if line in skip or _OTHER_LABEL_RE.match(line):
            continue
        if not re.search(r"[A-Za-z]{2,}", line):
            continue
        if _DATE_ANY_RE.fullmatch(line.strip()) or _line_amount(line):
            continue
        return line
    return None



def extract_fields(text: str, *, filename: str = "") -> DocumentFields:
    """Pull the structured fields out of one document's extracted text.

    *filename* is only a hint (``scan_receipt.jpg`` says "this is a receipt"), which
    is why the rules that have no label to go on - the supplier header, a bare
    invoice number - are only tried for a document that looks like an invoice or a
    receipt at all.
    """
    lines = _lines(text)
    body = str(text or "")
    invoice_like = looks_like_invoice(body, filename)
    found: dict[str, FieldValue] = {}

    # --- supplier: a label, else the header line --------------------------
    found[FIELD_SUPPLIER] = FieldValue(FIELD_SUPPLIER)
    value, line = _labelled(
        lines, SUPPLIER_LINE_RE, lambda raw: clean_text_value(FIELD_SUPPLIER, raw)
    )
    if value:
        found[FIELD_SUPPLIER] = FieldValue(FIELD_SUPPLIER, value, CONFIDENCE_LABELLED, line)
    elif invoice_like:
        header = _header_line(lines, skip=set())
        name = _clean_supplier_name(header)
        found[FIELD_SUPPLIER] = FieldValue(
            FIELD_SUPPLIER, name, CONFIDENCE_GUESSED if name else None, header
        )

    # --- invoice number: a label, else a token that looks like one --------
    value, line = _labelled(lines, INVOICE_NUMBER_LINE_RE, clean_identifier)
    if not value and invoice_like:
        for candidate in lines:
            match = _IDENTIFIER_TOKEN_RE.search(candidate)
            if match and (value := clean_identifier(match.group("value"))):
                line = candidate
                break
    found[FIELD_INVOICE_NUMBER] = FieldValue(
        FIELD_INVOICE_NUMBER, value, CONFIDENCE_LABELLED if value else None, line
    )

    # --- date: a label, else the first date on the page -------------------
    value, line = _labelled(lines, DATE_LINE_RE, normalize_date)
    labelled_date = bool(value)
    if not value:
        for candidate in lines:
            match = _DATE_ANY_RE.search(candidate)
            if match and (value := normalize_date(match.group(0))):
                line = candidate
                break
    found[FIELD_DOCUMENT_DATE] = FieldValue(
        FIELD_DOCUMENT_DATE,
        value,
        (CONFIDENCE_LABELLED if labelled_date else CONFIDENCE_OBVIOUS) if value else None,
        line,
    )

    # --- total amount, and the currency written next to it ----------------
    amount = line = None
    currency = None
    labelled_total = False
    for index, candidate in enumerate(lines):
        match = TOTAL_LINE_RE.match(candidate)
        if not match:
            continue
        amount, currency = _first_amount(match.group("value") or "")
        if amount is None and index + 1 < len(lines):
            amount, currency = _first_amount(lines[index + 1])
        if amount:
            line = candidate
            labelled_total = True
            break
        amount = None
    if not amount:
        best: tuple[str, str | None, str] | None = None
        for candidate in lines:
            for match in _AMOUNT_CANDIDATE_RE.finditer(candidate):
                raw = match.group("number")
                if _YEAR_LIKE_RE.match(raw.replace(" ", "")):
                    continue
                symbol, code = match.group("symbol"), match.group("code")
                if (
                    not re.search(r"[.,]\d{1,2}$", raw)
                    and not symbol
                    and not _currency_near(None, code)
                ):
                    continue
                parsed = parse_amount(raw)
                if parsed and (best is None or Decimal(parsed) > Decimal(best[0])):
                    best = (parsed, _currency_near(symbol, code), candidate)
        if best:
            amount, currency, line = best
    found[FIELD_TOTAL_AMOUNT] = FieldValue(
        FIELD_TOTAL_AMOUNT,
        amount,
        (CONFIDENCE_LABELLED if labelled_total else CONFIDENCE_GUESSED) if amount else None,
        line,
    )

    # --- currency: next to the total, else what the document mentions -----
    code = currency or _common_currency(body)
    beside_total = labelled_total and bool(currency)
    if not code:
        found[FIELD_CURRENCY] = FieldValue(FIELD_CURRENCY)
    else:
        found[FIELD_CURRENCY] = FieldValue(
            FIELD_CURRENCY,
            code,
            CONFIDENCE_LABELLED if beside_total else CONFIDENCE_GUESSED,
            line if beside_total else None,
        )



    return DocumentFields(tuple(found.get(key, FieldValue(key)) for key in FIELD_ORDER))


# ---------------------------------------------------------------------------
# review validation - the review form and POST /api/review/save share this
# ---------------------------------------------------------------------------
#: What to tell a reviewer whose value could not be read.
FIELD_ERRORS: dict[str, str] = {
    FIELD_DOCUMENT_DATE: "Use a date such as 2026-03-15 or 15.03.2026.",
    FIELD_TOTAL_AMOUNT: "Use a number such as 128.50 or 1.234,56.",
    FIELD_CURRENCY: "Use a three letter code such as EUR or USD.",
    FIELD_INVOICE_NUMBER: "Use letters, digits and - / . only.",
    FIELD_SUPPLIER: "Use a name of at most 255 characters.",
}


def validate_field(key: str, raw: object) -> tuple[str | None, str | None]:
    """``(value, error)`` for one submitted field; a blank value clears it.

    The same function backs both the review form and ``POST /api/review/save``, so a
    corrected value can never be stored differently through one route or the other.
    The message quotes what was typed, because the form keeps the *previous* value on
    a rejection - the excerpt is what tells the reviewer what they actually sent.
    """
    text = _clean_value(raw)
    if text is None:
        return None, None

    kind = FIELD_KINDS.get(key, "text")
    if kind == "date":
        value = normalize_date(text)
    elif kind == "amount":
        value = parse_amount(text)
    elif kind == "currency":
        value = normalize_currency(text)
    elif key == FIELD_INVOICE_NUMBER:
        value = clean_identifier(text)
    else:
        value = clean_text_value(key, text)

    if value:
        return value, None
    return None, f"Got {_excerpt(text)}. {FIELD_ERRORS.get(key, 'That value could not be used.')}"


def _excerpt(text: str, limit: int = 24) -> str:
    """``'not a number'`` - the submitted value, short and quoted for a message."""
    return repr(text if len(text) <= limit else f"{text[: limit - 3]}...")



def validate_fields(
    values: Mapping[str, object] | None = None, *, base: DocumentFields | None = None
) -> tuple[DocumentFields, dict[str, str]]:
    """``(fields, errors)`` for a submitted set of corrections.

    ``values`` is what the reviewer (or the API caller) sent; a key that is absent
    keeps the value from *base* (the extracted fields), and a key that cannot be
    read keeps it too while its message lands in ``errors``.  Validation therefore
    never destroys data - it either replaces a value with the corrected one or
    reports why it could not.
    """
    provided = values or {}
    base = base or DocumentFields.empty()
    cleaned: list[FieldValue] = []
    errors: dict[str, str] = {}

    for key in FIELD_ORDER:
        if key not in provided:
            cleaned.append(FieldValue(key, base.value(key)))
            continue
        value, error = validate_field(key, provided[key])
        if error:
            errors[key] = error
        cleaned.append(FieldValue(key, base.value(key) if error else value))

    return DocumentFields(tuple(cleaned)), errors







