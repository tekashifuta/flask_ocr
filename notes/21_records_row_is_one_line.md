# The records table: one line per row, with the actions where the eye is

A single stored record used to stretch its row to **~300px** whenever the file name,
the *supplier / no. / date / total* summary or the text snippet was long: those cells
wrapped onto four or five lines and dragged the whole row - and the `.txt`/`Delete`
buttons in *Actions*, which stayed at the top of it - with them.

Every cell is now **one line tall**, everything in the row is centred on that line, and
the table scrolls sideways inside `.records-wrap` (which is what that wrapper was
always for) with *Actions* pinned to its right edge.

| | before | after |
|---|---|---|
| row height (1 row, long snippet) | **301px** | **38px** |
| cells that wrap | yes (fields, snippet) | no |
| buttons' centre vs. row centre | +22px of a 301px row (top) | 19px of a 38px row (**centre**) |
| page wider than the window at 1168px | **yes** (scrollWidth 1186) | no (1168) |

## What changed, file by file

| File | What it does |
|---|---|
| `templates/_records.html` | The file name, the summary and the snippet cells carry a `title` with the **full** text (the snippet's line breaks collapsed to spaces, so the tooltip and the HTML stay one line). The buttons moved into a `<div class="cell-actions-inner">` **inside** the `<td>`; the *Actions* `<th>` got `class="cell-actions"` so the CSS can pin the column without reaching for `:last-child`. |
| `static/css/style.css` | New *"stored records"* rules: `.cell-file`/`.cell-fields`/`.cell-preview` are `nowrap` + `overflow: hidden` + `text-overflow: ellipsis` with a `max-width`; the row padding is `.5rem → .3rem`; `.cell-actions-inner` is the flex row the buttons sit in; `th.cell-actions`/`td.cell-actions` are `position: sticky; right: 0` with their own background, a hover colour and a hairline. `main.layout`'s grid items now take `min-width: 0`. |
| `tests/test_sqlite.py` | `test_records_row_keeps_the_full_cell_text_when_it_is_truncated` - the hover text and the button wrapper are asserted on the rendered page (306 → **307** passing). |

## Decisions worth writing down

* **Cut off, don't wrap - and keep the whole text in a `title`.** The information is not
  lost: hovering a cell shows it, the record page shows it in full, and `.xlsx`/`.json`
  always had it. That is the trade for a row a ledger can be scanned down.
* **The buttons are wrapped *inside* the cell.** `display: flex` on a `<td>` is what the
  old rule did, and it drops the table-cell box - so `vertical-align: middle` stopped
  applying and the buttons hung at the top (measured: centre at y=22 of a 301px row).
  With the flex wrapper inside, the `<td>` stays a table cell, `vertical-align: middle`
  centres it, and the measurement reads y=19 of a 38px row - exactly the text's centre.
* **`title`, not a tooltip bubble.** The page has a `?`-tooltip component, but a cell
  needs the *native* tooltip: it follows the pointer across ten columns, costs no
  JavaScript, and works with the table's own hover. The snippet's newlines are replaced
  because a newline inside an attribute becomes a multi-line native tooltip and a
  multi-line attribute in the source.
* **Caps trimmed from 22/24/26rem to 18/20/20rem.** With `white-space: nowrap` a cell's
  minimum width is its `max-width` (not its longest word, as when it could wrap), so the
  caps now decide how wide the table gets: 18/20/20rem is a file name, a summary and a
  snippet that stay informative on one line, and ~150px narrower than before.
* **`min-width: 0` on the layout's grid items.** A grid item defaults to
  `min-width: auto`, so one wide row used to stretch *the card* past the window (that is
  why the panel was cut off on the right in the old screenshots, and why the page had a
  horizontal scrollbar at 1168px **and** at 784px even before this change). Zero keeps
  the card at the width of the layout and lets the table scroll in `.records-wrap`.
  Nothing else gets wider: `pre.ocr-text` wraps its text (`white-space: pre-wrap`) and
  the other wide things already had `word-break`/`max-width: 100%`.
* ***Actions* is `position: sticky`.** The row is still wider than the card on a normal
  screen (the columns that cannot break - numbers, date, badges - take ~550px on their
  own), so the table scrolls; pinning the buttons means *Delete* is always one click
  away instead of one scroll away. A sticky cell paints its own `--surface` background
  (otherwise the row shows through it) and needs the hover colour spelled out again
  (`.records tr:hover td.cell-actions`), or a hovered row would keep one bright cell.
  The 1px `inset` hairline marks the cut, so the half-hidden letters of the column
  underneath read as a boundary rather than as a glitch.

## Verification

Rendered through the Flask test client with a SQLite store (one record whose file name,
summary and first 140 characters are all long) and measured in headless Chrome
(`--headless --window-size=… --screenshot`, a probe on `load` writing
`getBoundingClientRect()` numbers into the page):

* 1184px window: `pageOverflows: false`, `rowHeight: 38`, `anyCellWraps: false`, every
  cell `h: 38, cy: 19`, both buttons `h: 28, cy: 19`; at `scrollLeft: 403` (the row's
  end) `stickyActions: { visible: true, pinnedAtTheRightEdge: 0 }`.
* 800px and 640px windows: same row, page still exactly as wide as the window.
* The *"No stored record matches that search"* row is untouched by the sticky rules
  (the class is on the button cell, not on `:last-child`).
* `pytest -q`: **307 passed**.

**Limitation to note:** a row that is longer than the card scrolls inside
`.records-wrap` - at ~1100px that means the last part of the *Snippet* column is behind
the pinned *Actions*. The row's own scrollbar is the way to it, the native tooltip has
the text, and the record page has all of it; the alternative (letting four or five
columns wrap) is the 300px row this change removed. The measurements above are from
**headless Chrome**; with `border-collapse: collapse` an older Firefox can paint a
sticky cell's collapsed border inconsistently, which would only cost the 1px hairline.
