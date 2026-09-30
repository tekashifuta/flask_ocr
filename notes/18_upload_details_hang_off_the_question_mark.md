# *What happens to your file* hangs off the question mark now

The upload page had two places to read about the same thing: the `?` beside the heading
(note 16) and, after the form, a folded panel titled *What happens to your file* with five
steps and a table of limits (note 17). The panel is gone. Its title, its steps and its
limits all live in the `?` popup now, and the `?` sits at the lower right of the heading
row with the popup opening down and to the right.

```html
<div class="panel-head">
  <h1>Extract data from images and PDFs</h1>
  <div class="help-tip">
    <button class="help-tip-button" type="button" aria-describedby="upload-help"
            aria-expanded="false">?</button>
    <div class="help-tip-popup" id="upload-help" role="tooltip">
      <p class="help-tip-title">What happens to your file</p>
      <p>Upload one or more <strong>JPG</strong> …</p>
      <ol class="steps">…</ol>
      <dl class="facts">…</dl>
    </div>
  </div>
</div>
```

## What changed, file by file

| File | What it does |
|---|---|
| `templates/index.html` | The `<details>` panel is deleted; its title, steps and limits moved into `.help-tip-popup`. The `?` is no longer *inside* the `<h1>`: the heading and the icon are the two items of a `.panel-head` flex row. |
| `static/css/style.css` | `.panel-head` is a flex row with `align-items: flex-end`, so the heading takes the width that is left and the icon is pinned to the bottom right of it. The popup is anchored to the panel head (`right: 0; width: min(30rem, 100%)`), capped in height (`max-height: calc(100vh - 15rem)`) and scrolls itself. The caret moved from `.help-tip-popup::before` to `.help-tip::after`, and the `max-width: 1160px` fallback is gone. |
| `tests/test_validation.py` | `test_the_side_panels_fold_away` became `test_only_the_store_panel_folds_away` (one accordion is left) and a new `test_the_upload_explanation_hangs_off_the_question_mark` pins the popup to its `?`. |
| `static/js/app.js` | Untouched - `initHelpTips()` finds the same `.help-tip` / `.help-tip-button` pair, so tap, Escape and click-outside still work. |

## Decisions worth writing down

* **The heading and its `?` are one flex row.** Note 16 rejected a flex row because at
  ~390px the icon was pushed onto a line of its own, far from the words; that is exactly
  what is wanted here - *lower right*, not *after the last word*. `flex: 1 1 0; min-width: 0`
  on the `<h1>` (the trick the accordion summary already uses, note 17) is what keeps the
  two on one row: the heading is sized from the leftover width, so it wraps its own text
  while the icon stays put. Checked in headless Chrome at 1400/1000/700/520px - the icon's
  right edge *is* the head's right edge and its bottom edge *is* the heading's bottom edge
  at every one of them. At 390px (the page rendered in a 390px iframe, since Chrome will
  not open a window that narrow) the heading wraps to two lines and the `?` sits beside the
  second one.
* **The popup is anchored to the panel head, not to the icon.** The first attempt kept the
  old `right: 0` against the icon with a `100vw` width cap, and the measurement showed the
  popup starting at **x = -5** on a narrow viewport: `100vw` counts the scrollbar, which the
  absolute box does not get. With `position: static` on `.help-tip` the popup (and the
  caret) are measured against `.panel-head` instead, where `100%` is the panel's own content
  width - the one box the popup cannot escape. The panel head's right edge and the icon's
  right edge are the same line, which is why the caret still points at the `?`.
* **A 675px explanation is not a tooltip, so the popup scrolls itself.** `max-height:
  calc(100vh - 15rem)` (the footer, the site header and the heading row are about that
  tall) plus `overflow-y: auto`: measured, the popup is 563px tall on a 900px window and
  483px on a 725px one, always inside the viewport and never dragging the page with it.
  `scrollbar-width: thin` + `scrollbar-color` keep the OS scrollbar from dominating a
  30rem card, and `overscroll-behavior: contain` is what stops the page from scrolling out
  from under the pointer once the popup's text ends.
* **The caret hangs off the icon but is measured from the head.** It cannot live on the
  popup any more: the popup scrolls, and a scrolling box clips its own `::before`. It is
  drawn by `.help-tip::after` (so it exists exactly where there is an icon) with the same
  `right: .95rem` the popup uses; its computed `top` is 47.9px - the head's 46px plus
  `.13rem` - which is where the popup's top edge is.
* **The hidden text is still in the page.** It only changes `opacity` and `visibility`, so
  `aria-describedby` still reaches it, Ctrl+F still finds it, and the tests can still assert
  *"review them before anything is saved"*, the steps and the limits.
* **The whole popup is the description.** `aria-describedby` points at the popup itself, so a
  screen reader announces the title, the paragraph, the five steps and the limits as one
  description of the `?`. Long-winded, but it is the only way in: nothing inside a
  `visibility: hidden` popup is focusable, so a keyboard user's Tab leaves the button and the
  popup closes behind it. Note 16 had the same trade-off with one sentence instead of a list.
* **Nothing was trimmed - and that has one visible cost.** The popup keeps the paragraph
  that used to be the whole tooltip even though the steps below it say the same things, so
  on a 900px-tall window the last two limit rows need ~110px of scrolling; on a 1080p
  screen (viewport ≈ 950px, cap 710px) all 675px of it fits. Dropping that paragraph is a
  one line change if the scrolling is not wanted.
