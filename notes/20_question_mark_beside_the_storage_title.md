# The question mark sits beside the storage title now

`/database`'s `?` used to be the third item of the panel head: after the eyebrow, the title,
the *Connected* badge and the *Records view* button, flush against the card's right edge and
bottom-aligned with the heading (note 19). On a card whose head ends in a badge and a button
that read as if the icon belonged to the *button row* - it sat alone under *Records view* -
not to the words. It is now the second item of the title row, right after `MySQL storage`
and centred on the title's own line box.

```html
<div class="panel-head">
  <div class="panel-head-text">
    <p class="eyebrow">Database storage</p>
    <div class="panel-head-title">
      <h1>MySQL storage</h1>
      <div class="help-tip"> ? + .help-tip-popup#storage-help </div>
    </div>
  </div>
  <div class="result-actions"> badge + Records view </div>
</div>
```

## What changed, file by file

| File | What it does |
|---|---|
| `templates/database.html` | A `.panel-head-title` row now holds the `<h1>` and the `.help-tip` (button + popup); the head is left with two items, the text block and `.result-actions`, so the badge and the button keep their place and their DOM order. The popup's paragraphs were re-indented with the row, and one phrase changed: "the *records view* button **above**" became "**in the header**", because the popup no longer opens below that button. |
| `static/css/style.css` | `.panel-head-title` is a new shrink-to-fit flex row (`position: relative`, `align-items: center`, `gap: .5rem`, `width: fit-content`, `max-width: 100%`) with `.panel-head-title > h1 { flex: 0 1 auto }`. `.help-tip` lost its `margin-left: auto` - it is `.panel-head > .help-tip` now, so an icon that is a direct child of a head still hugs the right edge. `.panel-head-title .help-tip-popup` re-anchors the popup (`right: auto; left: 0; width: min(30rem, calc(100vw - 6rem))`) and `.panel-head-title .help-tip::after` puts the caret under the icon (`.325rem` from the row's right edge). Three stale comments were rewritten. |
| `static/js/app.js`, `templates/index.html` | Untouched - `initHelpTips()` finds the same `.help-tip` / `.help-tip-button` pair wherever it sits, so tap, Escape and click-outside still work. |
| `tests/test_database.py` | New `test_the_storage_question_mark_sits_beside_the_title`: the `<h1>` and the `.help-tip` are both inside the one `.panel-head-title`, the icon comes after the title and before `.result-actions`, and there is exactly one title row. `test_the_storage_details_hang_off_the_question_mark` still pins the popup, its `aria-describedby` and both paragraphs, unchanged. |

## Decisions worth writing down

* **The row is only as wide as the words.** `width: fit-content` (capped at `100%`) plus
  `flex: 0 1 auto` on the `<h1>`: inside a title row the heading contributes its own words to
  the row's width instead of taking the width that is left over of the head, so the icon lands
  exactly `.5rem` after the last word - measured `icon.left - title.right = 8` at
  1400/1000/700/560/390px. The row being exactly as wide as its content, its right edge *is*
  the icon's right edge, which is what lets the caret and the popup be measured against it.
* **`position: relative` on the row is what re-anchors the popup and the caret.** The icon is
  still deliberately *not* positioned: in Chrome the absolutely positioned popup and `::after`
  are measured against the nearest positioned ancestor, so the row (not the panel head, as
  notes 18/19 had it) becomes their containing block - and the popup and caret formulas
  (`top: calc(100% + .55rem)`, `top: calc(100% + .13rem)`) did not have to change. The row's
  right edge is the icon's own right edge, so the caret needs one override: its tip belongs
  at the icon's centre, i.e. `1.45rem / 2` in from that edge, and its box is `.8rem` wide, so
  `right: calc(1.45rem / 2 - .4rem)` = `.325rem`. Measured: `caret tip x == icon centre x` at
  every width (444.3 == 444.3 at 1400px, 208.3 == 208.3 at 390px).
* **`100%` would have been the words.** The base rule caps the popup at `min(30rem, 100%)`,
  which against a 242px-wide title row would have squeezed the explanation to a fifth of its
  size, so this popup caps itself against the viewport instead (`calc(100vw - 6rem)`):
  `min()` still gives 30rem (480px) from a 576px window up. With the popup's left edge on the
  row's left edge - the start of the title - it stays inside the panel *and* the window:
  480px wide at 1400px (panel content 212.5..1156.5), 448px at 544px, 294px at 390px
  (panel content 33.6.., popup 34.6..328.6 inside a 390px viewport). `document.scrollWidth`
  equals `clientWidth` at 1400/1000/700/560px, and at 390px the widest boxes over the viewport
  are the two `<details>` accordions (16..571) - the pre-existing
  `instance\mysql_connection.json` path note 19 reported - not this card.
* **The caret hangs under the title line, not on the icon's bottom edge.** Centring the icon
  on the heading's line box is what makes it look lined up with the words (`title centre y ==
  icon centre y` at every width), and that costs the caret its landing place: the icon's
  bottom then sits half a line-height above the row's bottom edge - 10.7px at the 1.85rem
  title, 4.6px below ~830px where the font clamps to 1.35rem - while the caret's tip stays on
  the row's bottom edge (193.0 against `row.bottom = 193.6` at 1400px), which is where its
  diamond still overlaps the popup's top edge and so still reads as the popup's arrow.
  `align-items: flex-end` was rejected: it would put the caret exactly on the icon but the `?`
  8-11px *below* the title's visual centre, which is not the alignment that was asked for.
* **The upload page did not move.** Moving `margin-left: auto` from `.help-tip` to
  `.panel-head > .help-tip` keeps the upload card's icon flush right and bottom-aligned -
  re-measured after the change: `icon.right - head.right = 0` and `icon.bottom - h1.bottom = 0`
  at 1400/1000/700/390px (390px: icon `332.2,182.9 .. 355.4,206.1` against a head ending at
  `355.4,206.1`, the same numbers note 19 quoted), with `popup.right - head.right = 0`.
* **On a phone the badge and the button drop below the title** (the existing
  `max-width: 640px` rule gives `.result-actions` a line of its own) and the popup opens over
  them - the same overlay trade-off notes 18/19 accepted on both cards. The heading itself
  stays one line beside its icon down to 390px, which is the *point* of the change.
* **Measured in headless Chrome** on the real rendered page (the render plus a measuring
  script, `--dump-dom` and `--screenshot`, and a 390px iframe because Chrome will not open a
  window that narrow). 1000px: `title 65,141.9 .. 247.4,181.5`, `icon 255.4,150.1 ..
  278.6,173.3`, `popup 65,190.3 .. 545,556.2` against a panel content of `64..905`. 390px:
  `title 34.6,160.9 .. 188.7,194.4`, `icon 196.7,166 .. 219.9,189.2`, `popup 34.6,203.2 ..
  328.6,789.2` - the screenshot of the open state shows the caret tucked under the `?` and the
  card opening from the title's own left edge.

