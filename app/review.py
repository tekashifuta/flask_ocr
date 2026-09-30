"""Review step: what OCR proposed, what the reviewer changed, what was stored.

The browser flow is **upload -> review -> store**.  Nothing reaches the database
before a human has seen the structured fields next to the extracted text, which is
the point of ``templates/review.html``: OCR reads a ``5`` as an ``S`` often enough
to matter when the number ends up in a ledger.

This module owns the small model behind that page - :class:`ReviewDocument` (one
extracted document plus its fields) and :class:`ReviewBatch` (a whole upload, one
file or twenty) - and the parsing and validation of what the form posted.  The OCR
runs in ``routes``, the SQL in the storage layer, and the rules for a single value
in :mod:`app.fields`; nobody duplicates anybody else's job here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .fields import FIELD_ORDER, DocumentFields, validate_fields
from .ocr import ExtractionResult

#: Name of the per-document inputs in the review form: ``supplier_0``,
#: ``invoice_number_0``, ... and the ``include_0`` checkbox that selects a row.
FIELD_INPUT = "{key}_{index}"
INCLUDE_INPUT = "include_{index}"
#: The repeated hidden field that says which cached result each row belongs to.
RESULT_ID_INPUT = "result_id"


def checkbox_value(values: Sequence[str] | None, default: bool = False) -> bool:
    """``1``/``on``/``true`` = yes for an ``<input type="checkbox">`` pair.

    The upload and review forms submit every checkbox **twice**: a hidden ``0`` (so
    an unticked box still sends something) and the checkbox itself.  *Any* truthy
    value therefore means "checked"; a request that sends no such field at all keeps
    *default*.
    """
    if not values:
        return default
    return any(str(value).strip().lower() in {"1", "true", "yes", "on"} for value in values)


@dataclass(frozen=True)
class ReviewDocument:
    """One extracted document, with the fields as extracted *and* as reviewed."""

    result_id: str
    result: ExtractionResult
    #: What the parser proposed - the confidence badges describe this.
    extracted: DocumentFields
    #: What will be stored; equal to *extracted* until a reviewer changes it.
    fields: DocumentFields
    #: Not ticked in the review form: keep this document out of the store.
    included: bool = True
    #: ``(field key, message)`` pairs the reviewer still has to fix.
    field_errors: tuple[tuple[str, str], ...] = ()
    #: Set after a submit: the new record, or why there is none.
    record_id: int | None = None
    error: str | None = None

    # -- what the template prints -----------------------------------------
    @property
    def filename(self) -> str:
        return self.result.filename

    @property
    def kind(self) -> str:
        return self.result.kind

    @property
    def page_count(self) -> int:
        return self.result.page_count

    @property
    def char_count(self) -> int:
        return self.result.char_count

    @property
    def word_count(self) -> int:
        return self.result.word_count

    @property
    def confidence(self) -> float | None:
        return self.result.confidence

    @property
    def preview_data_uri(self) -> str | None:
        return self.result.preview_data_uri

    @property
    def is_empty(self) -> bool:
        return self.result.is_empty

    # -- review state ------------------------------------------------------
    @property
    def has_field_errors(self) -> bool:
        return bool(self.field_errors)

    def error_for(self, key: str) -> str | None:
        """The message for one field, if the reviewer's value was unusable."""
        for field_key, message in self.field_errors:
            if field_key == key:
                return message
        return None

    @property
    def corrected_keys(self) -> tuple[str, ...]:
        """The fields whose stored value differs from what OCR proposed."""
        return tuple(
            key
            for key in FIELD_ORDER
            if self.fields.get(key).value != self.extracted.get(key).value
        )

    @property
    def is_corrected(self) -> bool:
        return bool(self.corrected_keys)

    @property
    def is_saved(self) -> bool:
        return self.record_id is not None


@dataclass(frozen=True)
class ReviewBatch:
    """One upload, as the review page sees it."""

    documents: tuple[ReviewDocument, ...]
    #: Upload level messages (a file that was rejected, an expired result, ...).
    notices: tuple[str, ...] = ()
    #: True once ``/review/save`` has answered for this batch.
    submitted: bool = False
    #: Why nothing could be stored at all (no store connected, ...).
    database_error: str | None = None

    def __len__(self) -> int:
        return len(self.documents)

    @property
    def count(self) -> int:
        return len(self.documents)

    @property
    def is_single(self) -> bool:
        return len(self.documents) == 1

    @property
    def selected(self) -> tuple[ReviewDocument, ...]:
        """The documents the submit asked to store."""
        return tuple(document for document in self.documents if document.included)

    @property
    def skipped(self) -> tuple[ReviewDocument, ...]:
        """The documents the reviewer unticked."""
        return tuple(document for document in self.documents if not document.included)

    @property
    def saved(self) -> tuple[ReviewDocument, ...]:
        return tuple(document for document in self.documents if document.is_saved)

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def failed(self) -> tuple[ReviewDocument, ...]:
        return tuple(document for document in self.documents if document.error)

    @property
    def field_error_count(self) -> int:
        return sum(len(document.field_errors) for document in self.documents)

    @property
    def corrected_count(self) -> int:
        return sum(1 for document in self.documents if document.is_corrected)

    @property
    def is_valid(self) -> bool:
        """No value left to fix - the only state in which saving may run."""
        return not self.field_error_count


def submitted_result_ids(form: Mapping[str, object]) -> tuple[str, ...]:
    """The ``result_id`` values the review form posted, in document order.

    ``request.form`` is a ``MultiDict`` and hands back every value in the order the
    browser sent them, which is the order of the cards on the page - that is what
    makes ``supplier_0``/``supplier_1`` line up with the right document.
    """
    getlist = getattr(form, "getlist", None)
    if getlist is None:  # a plain dict (JSON API, tests)
        value = form.get(RESULT_ID_INPUT)
        return (str(value),) if value else ()
    return tuple(str(value) for value in getlist(RESULT_ID_INPUT) if str(value).strip())


def submitted_field_values(
    form: Mapping[str, object], index: int, *, keys: Iterable[str] = FIELD_ORDER
) -> dict[str, object]:
    """``{field: text}`` for one document card - absent inputs are left out.

    A key that is not in the form at all is *not* returned, so
    :func:`app.fields.validate_fields` keeps the extracted value for it instead of
    reading a missing input as "clear this field".
    """
    values: dict[str, object] = {}
    for key in keys:
        name = FIELD_INPUT.format(key=key, index=index)
        if name in form:
            values[key] = form.get(name)
    return values


def _includes(form: Mapping[str, object], index: int) -> bool:
    """Was this document ticked?  A form without the checkbox keeps it included."""
    getlist = getattr(form, "getlist", None)
    values = getlist(INCLUDE_INPUT.format(index=index)) if getlist else None
    return checkbox_value(values, default=True)


def parse_form(
    form: Mapping[str, object], documents: Sequence[ReviewDocument]
) -> tuple[tuple[ReviewDocument, ...], tuple[str, ...]]:
    """Read a submitted review form into reviewed documents.

    Returns the documents (corrections applied, per field problems recorded) and the
    batch level notices.  Nothing is written here: saving stays the caller's
    decision, so a form holding an unusable date is re-rendered with the typo in it
    instead of being thrown away.
    """
    reviewed: list[ReviewDocument] = []
    notices: list[str] = []

    for index, document in enumerate(documents):
        fields, errors = validate_fields(
            submitted_field_values(form, index), base=document.extracted
        )
        if errors:
            notices.append(
                f"{document.filename}: {len(errors)} value(s) could not be read - "
                "see the highlighted field(s)."
            )
        reviewed.append(
            ReviewDocument(
                result_id=document.result_id,
                result=document.result,
                extracted=document.extracted,
                fields=fields,
                included=_includes(form, index),
                field_errors=tuple(sorted(errors.items())),
                error=document.error,
            )
        )
    return tuple(reviewed), tuple(notices)


def documents_from(
    pairs: Iterable[tuple[str, ExtractionResult]]
) -> tuple[ReviewDocument, ...]:
    """Build review rows for ``(result_id, result)`` pairs, fields pre-filled."""
    return tuple(
        ReviewDocument(
            result_id=result_id,
            result=result,
            extracted=result.structured_fields,
            fields=result.structured_fields,
        )
        for result_id, result in pairs
    )

