"""Excel export: a dependency free ``.xlsx`` writer plus the records workbook.

An ``.xlsx`` file is an OPC package - a ZIP holding a handful of XML parts - so a
small writer built on :mod:`zipfile` and :mod:`xml.sax.saxutils` is all this feature
needs.  No pandas, no openpyxl, nothing to install: the export runs wherever the
OCR pipeline already runs, and the file it produces opens in Excel, LibreOffice and
Numbers alike.

What the writer supports, and deliberately nothing more:

* several sheets, each with a typed header row, column widths, an optional frozen
  header and an auto filter;
* text cells (written as *inline strings*, so a file name like ``123`` stays text),
  plus integer and decimal cells with Excel's own number formats;
* a header style and wrapped text cells for the extracted content;
* the sanitising Excel requires: control characters that XML 1.0 forbids are
  dropped and a cell is capped at :data:`MAX_CELL_CHARS` characters, because Excel
  refuses to open a workbook that exceeds it.

:func:`records_workbook` is what the application uses it for: it turns the rows of
the records view into a workbook with *Records* (one row per record, full extracted
text included), *Pages* (one row per page of every exported record) and *Export*
(which filters produced the file) sheets.
"""

from __future__ import annotations

import io
import logging
import math
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

#: MIME type browsers/Excel expect when the attachment is offered.
XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_EXTENSION = ".xlsx"

#: Excel refuses to open a workbook holding a longer cell.
MAX_CELL_CHARS = 32_767
#: Sheet name limit (Excel rejects anything else, or refuses to open the file).
MAX_SHEET_NAME_CHARS = 31
_FORBIDDEN_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")
#: Spreadsheet geometry of an ``.xlsx`` file: 16,384 columns (XFD), 1,048,576 rows.
MAX_COLUMNS = 16_384
MAX_ROWS = 1_048_576
#: Characters that are not allowed in XML 1.0 - Excel rejects the whole part.
_ILLEGAL_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")

# -- XML namespaces of the parts we write -------------------------------
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
APP_PROPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CORE_PROPS_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

#: How a column is written; the mapping to a number format is in :data:`STYLES_XML`.
TEXT = "text"
INTEGER = "integer"
DECIMAL = "decimal"
KINDS = (TEXT, INTEGER, DECIMAL)

# -- cell style ids (indexes into the ``cellXfs`` table of ``xl/styles.xml``) --
_STYLE_DEFAULT = 0
_STYLE_HEADER = 1
_STYLE_TEXT = 2
_STYLE_WRAPPED = 3
_STYLE_INTEGER = 4
_STYLE_DECIMAL = 5


@dataclass(frozen=True)
class Column:
    """One column of a sheet: its header, its ``key`` and how values are written."""

    header: str
    #: Key of the record a cell takes its value from (see ``records_workbook``).
    key: str = ""
    #: Excel column width in characters (approximately).
    width: float = 16.0
    kind: str = TEXT
    #: Wrap long text instead of letting it overflow into the next cell.
    wrap: bool = False

    def __post_init__(self) -> None:  # pragma: no cover - programmer error only
        if self.kind not in KINDS:
            raise ValueError(f"Unknown column kind {self.kind!r}; use one of {KINDS}.")


@dataclass(frozen=True)
class Sheet:
    """A sheet: a header row plus one sequence of values per row."""

    name: str
    columns: Sequence[Column]
    rows: Sequence[Sequence[object]] = ()
    #: Keep the header row visible while scrolling.
    freeze: bool = True
    #: Excel's header dropdowns for filtering the exported table.
    autofilter: bool = True


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------
def column_letter(index: int) -> str:
    """One-based column index -> letters (``1`` -> ``A``, ``27`` -> ``AA``)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _text(value: object) -> str:
    """Make *value* a string XML can carry and Excel will accept."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ILLEGAL_XML_CHARS.sub("", text)
    if len(text) > MAX_CELL_CHARS:
        logger.debug("Truncating a cell of %s characters to %s", len(text), MAX_CELL_CHARS)
        text = f"{text[: MAX_CELL_CHARS - 1]}\u2026"
    return text


def _number(value: int | float) -> str | None:
    """Render a number for Excel, or ``None`` when it cannot be one.

    ``nan``/``inf`` have no representation in a spreadsheet cell, so the caller
    writes those as text instead (a silently empty cell would be worse).
    """
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return None
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def _column_width(width: object) -> str:
    """``38`` / ``6.5`` - the plainest form Excel accepts for ``<col width=...>``."""
    try:
        number = float(width)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - configuration guard
        number = 16.0
    return f"{max(1.0, min(number, 255.0)):g}"


def _unique_sheet_name(name: object, used: set[str]) -> str:
    """A legal, unique sheet name (Excel: <= 31 characters, no ``[]:*?/\\``)."""
    cleaned = _FORBIDDEN_SHEET_CHARS.sub(" ", str(name)).strip().strip("'").strip()
    base = (cleaned or "Sheet")[:MAX_SHEET_NAME_CHARS]
    candidate = base
    counter = 2
    while candidate.lower() in used:
        suffix = f" ({counter})"
        candidate = f"{base[: MAX_SHEET_NAME_CHARS - len(suffix)]}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


# ---------------------------------------------------------------------------
# XML parts
# ---------------------------------------------------------------------------
def _zip_info(name: str) -> zipfile.ZipInfo:
    """A package entry with a fixed timestamp, so equal input yields equal bytes."""
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _cell_xml(reference: str, value: object, style: int) -> str:
    """One ``<c>`` element - text as an inline string, numbers as a number."""
    if value is None or value == "":
        return f'<c r="{reference}" s="{style}"/>'
    if isinstance(value, bool):  # bool is an int subclass; spell it out instead
        value = "yes" if value else "no"
    if isinstance(value, (int, float)):
        number = _number(value)
        if number is not None:
            return f'<c r="{reference}" s="{style}"><v>{number}</v></c>'
    # ``xml:space="preserve"`` keeps leading/trailing whitespace of stored text.
    text = escape(_text(value))
    return (
        f'<c r="{reference}" s="{style}" t="inlineStr">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _cell_style(column: Column, value: object) -> int:
    """The style id for one cell: number formats, wrapped text, plain text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _STYLE_INTEGER if column.kind == INTEGER else _STYLE_DECIMAL
    if column.kind == INTEGER:
        return _STYLE_INTEGER
    if column.kind == DECIMAL:
        return _STYLE_DECIMAL
    return _STYLE_WRAPPED if column.wrap else _STYLE_TEXT


def _sheet_data_xml(columns: Sequence[Column], rows: Sequence[Sequence[object]]) -> str:
    """The header row plus one ``<row>`` per record."""
    header = "".join(
        _cell_xml(f"{column_letter(index)}1", column.header, _STYLE_HEADER)
        for index, column in enumerate(columns, start=1)
    )
    body = []
    for number, values in enumerate(rows, start=2):
        cells = "".join(
            _cell_xml(
                f"{column_letter(index)}{number}", value, _cell_style(column, value)
            )
            for index, (column, value) in enumerate(zip(columns, values), start=1)
        )
        body.append(f'<row r="{number}">{cells}</row>')
    return f'<sheetData><row r="1">{header}</row>{"".join(body)}</sheetData>'


def _cols_xml(columns: Sequence[Column]) -> str:
    """Column widths (Excel falls back to ~8.43 characters without them)."""
    return "<cols>" + "".join(
        f'<col min="{index}" max="{index}" width="{_column_width(column.width)}" '
        'customWidth="1"/>'
        for index, column in enumerate(columns, start=1)
    ) + "</cols>"


def _sheet_views_xml(freeze_header: bool) -> str:
    """Freeze row 1 so the headers stay visible while scrolling."""
    if not freeze_header:
        return '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    return (
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        "</sheetView></sheetViews>"
    )


def _sheet_xml(
    sheet: Sheet, columns: Sequence[Column], rows: Sequence[Sequence[object]]
) -> str:
    """One ``xl/worksheets/sheetN.xml`` part."""
    parts = [
        _XML_DECLARATION,
        f'<worksheet xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">',
        _sheet_views_xml(sheet.freeze),
        '<sheetFormatPr defaultRowHeight="15"/>',
        _cols_xml(columns),
        _sheet_data_xml(columns, rows),
    ]
    if sheet.autofilter and rows:
        # Element order matters in sheet XML: autoFilter before pageMargins.
        parts.append(f'<autoFilter ref="A1:{column_letter(len(columns))}{len(rows) + 1}"/>')
    parts.append(
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" '
        'header="0.3" footer="0.3"/>'
    )
    parts.append("</worksheet>")
    return "".join(parts)


def _content_types_xml(sheet_count: int) -> str:
    """``[Content_Types].xml`` - declares every part of the package."""
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        f'{_XML_DECLARATION}<Types xmlns="{CONTENT_TYPES_NS}">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType='
        '"application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        f"{sheets}</Types>"
    )


def _package_rels_xml() -> str:
    """``_rels/.rels`` - points the package at the workbook and the properties."""
    return (
        f'{_XML_DECLARATION}<Relationships xmlns="{PACKAGE_REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type='
        f'"{PACKAGE_REL_NS}/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type='
        f'"{REL_NS}/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_rels_xml(sheet_count: int) -> str:
    """``xl/_rels/workbook.xml.rels`` - sheets first, then the style table."""
    sheets = "".join(
        f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        f'{_XML_DECLARATION}<Relationships xmlns="{PACKAGE_REL_NS}">'
        f"{sheets}"
        f'<Relationship Id="rId{sheet_count + 1}" Type="{REL_NS}/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheets: Sequence[Sheet]) -> str:
    """``xl/workbook.xml`` - the sheet order Excel shows in its tab bar."""
    entries = "".join(
        f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        f'{_XML_DECLARATION}<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
        '<workbookPr/>'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        f"<sheets>{entries}</sheets>"
        '<calcPr calcId="191029"/>'
        "</workbook>"
    )


def _app_xml(title: str) -> str:
    """``docProps/app.xml`` - the file's extended properties.

    The elements follow the schema order (``Manager``/``Company`` before
    ``Application``); Excel is forgiving, but LibreOffice validates more strictly.
    """
    return (
        f'{_XML_DECLARATION}<Properties xmlns="{APP_PROPS_NS}">'
        f"<Manager>{escape(_text(title))}</Manager>"
        "<Company>Flask OCR</Company>"
        "<Application>Flask OCR</Application>"
        "<DocSecurity>0</DocSecurity>"
        "<ScaleCrop>false</ScaleCrop>"
        "<LinksUpToDate>false</LinksUpToDate>"
        "<SharedDoc>false</SharedDoc>"
        "<HyperlinksChanged>false</HyperlinksChanged>"
        "<AppVersion>16.0300</AppVersion>"
        "</Properties>"
    )


def _core_xml(title: str, creator: str, created: datetime) -> str:
    """``docProps/core.xml`` - who and when, in the format Office expects."""
    stamp = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'{_XML_DECLARATION}<cp:coreProperties xmlns:cp="{CORE_PROPS_NS}" '
        f'xmlns:dc="{DC_NS}" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(_text(title))}</dc:title>"
        f"<dc:creator>{escape(_text(creator))}</dc:creator>"
        f"<cp:lastModifiedBy>{escape(_text(creator))}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )



#: The style table: two fonts (plain, header), the fills Excel insists on, a thin
#: bottom border for the header row and the six cell formats referenced above.
STYLES_XML = (
    f'{_XML_DECLARATION}<styleSheet xmlns="{MAIN_NS}">'
    '<fonts count="2">'
    '<font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/>'
    '<scheme val="minor"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/>'
    '<family val="2"/><scheme val="minor"/></font>'
    "</fonts>"
    '<fills count="3">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="2">'
    "<border><left/><right/><top/><bottom/><diagonal/></border>"
    '<border><left/><right/><top/><bottom style="thin"><color rgb="FFD0D7DE"/>'
    "</bottom><diagonal/></border>"
    "</borders>"
    '<cellStyleXfs count="1">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    # 0 - default (never referenced directly, but Excel expects the first entry)
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    # 1 - header row
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" '
    'applyFill="1" applyBorder="1" applyAlignment="1">'
    '<alignment vertical="center"/></xf>'
    # 2 - plain text, top aligned
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment vertical="top"/></xf>'
    # 3 - wrapped text (the extracted content)
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    # 4 - integer with a thousands separator (`#,##0`)
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" '
    'applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>'
    # 5 - decimal (`0.00`) for confidence values
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" '
    'applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '<dxfs count="0"/>'
    '<tableStyles count="0" defaultTableStyle="TableStyleMedium2" '
    'defaultPivotStyle="PivotStyleLight16"/>'
    "</styleSheet>"
)


def _prepare_sheets(sheets: Sequence[Sheet]) -> list[Sheet]:
    """Apply the rules Excel enforces when it opens a file (names, geometry)."""
    used: set[str] = set()
    prepared: list[Sheet] = []
    for sheet in sheets:
        prepared.append(
            replace(
                sheet,
                name=_unique_sheet_name(sheet.name, used),
                columns=sheet.columns[:MAX_COLUMNS],
                rows=[row[:MAX_COLUMNS] for row in sheet.rows[:MAX_ROWS]],
            )
        )
    return prepared


def workbook_bytes(
    sheets: Sequence[Sheet],
    *,
    title: str = "Flask OCR export",
    creator: str = "Flask OCR",
    created: datetime | None = None,
) -> bytes:
    """Render *sheets* as an ``.xlsx`` file and return its bytes.

    Everything happens in memory - nothing is written to disk - and the same input
    always produces the same bytes (the ZIP entries use a fixed timestamp), which
    keeps tests and diffs honest.
    """
    prepared = _prepare_sheets(sheets)
    parts: list[tuple[str, str]] = [
        ("[Content_Types].xml", _content_types_xml(len(prepared))),
        ("_rels/.rels", _package_rels_xml()),
        ("docProps/app.xml", _app_xml(title)),
        ("docProps/core.xml", _core_xml(title, creator, created or datetime.now(timezone.utc))),
        ("xl/workbook.xml", _workbook_xml(prepared)),
        ("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(prepared))),
        ("xl/styles.xml", STYLES_XML),
    ]
    parts += [
        (f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet, sheet.columns, sheet.rows))
        for index, sheet in enumerate(prepared, start=1)
    ]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts:
            archive.writestr(_zip_info(name), payload)
    logger.debug("Built an .xlsx workbook with %s sheet(s)", len(prepared))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# the records workbook
# ---------------------------------------------------------------------------
#: ``Records`` sheet: the columns of the records view plus the full text.
RECORD_COLUMNS: tuple[Column, ...] = (
    Column("#", key="id", width=8, kind=INTEGER),
    Column("File name", key="filename", width=38),
    Column("Uploaded (UTC)", key="uploaded_at", width=21),
    Column("Type", key="kind", width=9),
    Column("Pages", key="page_count", width=8, kind=INTEGER),
    Column("Characters", key="char_count", width=11, kind=INTEGER),
    Column("Words", key="word_count", width=9, kind=INTEGER),
    Column("Confidence (%)", key="confidence", width=15, kind=DECIMAL),
    Column("Duration (ms)", key="duration_ms", width=13, kind=INTEGER),
    Column("Size (bytes)", key="size_bytes", width=12, kind=INTEGER),
    Column("Language", key="ocr_language", width=11),
    Column("Engine", key="engine_version", width=13),
    Column("Stored at", key="stored_at", width=21),
    Column("SHA-256", key="content_sha256", width=18),
    Column("Extracted text", key="content", width=90, wrap=True),
)

#: ``Pages`` sheet: one row per page of every exported record.
PAGE_COLUMNS: tuple[Column, ...] = (
    Column("Record #", key="extraction_id", width=9, kind=INTEGER),
    Column("File name", key="filename", width=34),
    Column("Page", key="page_number", width=7, kind=INTEGER),
    Column("Method", key="method", width=12),
    Column("Characters", key="char_count", width=11, kind=INTEGER),
    Column("Words", key="word_count", width=9, kind=INTEGER),
    Column("Confidence (%)", key="confidence", width=15, kind=DECIMAL),
    Column("Duration (ms)", key="duration_ms", width=13, kind=INTEGER),
    Column("Page text", key="content", width=90, wrap=True),
)

#: ``Export`` sheet: which filters produced this file.
FACT_COLUMNS: tuple[Column, ...] = (
    Column("Field", width=24),
    Column("Value", width=70, wrap=True),
)


def _rows(records: Sequence[Mapping], columns: Sequence[Column]) -> list[list[object]]:
    """One row per record, in the order the columns declare (by ``key``)."""
    return [[record.get(column.key) for column in columns] for record in records]


def records_workbook(
    records: Sequence[Mapping],
    *,
    pages: Sequence[Mapping] = (),
    facts: Sequence[tuple[str, object]] = (),
    title: str = "Stored OCR records",
) -> bytes:
    """Turn stored extraction rows into the workbook the records view offers.

    * **Records** - one row per stored extraction: the file name, the upload time,
      every statistic the record page shows, and the **full extracted text** (the
      records view itself only lists a snippet).
    * **Pages** - one row per page of those records, i.e. the
      ``ocr_extractions_pages`` table (the parent table plus ``_pages``).
      Added only when page rows were supplied.
    * **Export** - the filters, the schema and the row count, so a spreadsheet that
      travels by e-mail explains itself.

    ``pages`` rows are matched by the column keys above (``extraction_id``,
    ``filename`` when the caller knows it, ``page_number``, ...).
    """
    sheets = [Sheet("Records", RECORD_COLUMNS, _rows(records, RECORD_COLUMNS))]
    if pages:
        sheets.append(Sheet("Pages", PAGE_COLUMNS, _rows(pages, PAGE_COLUMNS)))
    if facts:
        sheets.append(
            Sheet(
                "Export",
                FACT_COLUMNS,
                [[label, value] for label, value in facts],
                autofilter=False,
            )
        )
    return workbook_bytes(sheets, title=title)

