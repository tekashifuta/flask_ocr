# The explanation under *Extract data from images and PDFs* is now a hover tooltip

The upload page used to open with a long paragraph under its heading:

```html
<h1>Extract data from images and PDFs</h1>
<p class="lede">
  Upload one or more <strong>JPG</strong>, <strong>PNG</strong> or <strong>PDF</strong>
  files (up to 10 per batch). Scanned pages and multi-page PDFs are handled page by page
  with Tesseract, the structured fields (supplier, invoice number, date, total) are read
  out of the text, and <strong>you review them before anything is saved</strong>.
</p>
```

It eats three lines of the first thing a user sees and pushes the drop zone down. It is
now behind a small question mark beside the title, revealed on hover.

## What changed, file by file

| File | What it does |
|---|---|
| `templates/index.html` | The heading keeps its text; the `?` is an inline `<span class="help-tip">` **inside the `<h1>`**, and the paragraph is its `.help-tip-popup` child with `role="tooltip"`, referenced by the button's `aria-describedby`. |
| `static/css/style.css` | A new *"a title and its question mark"* block: the round icon, the popup (with a caret), and the three ways to open it - `:hover`, `:focus-within` and `.is-open`. |
| `static/js/app.js` | `initHelpTips()`: CSS cannot toggle on a tap, so a click sets `.is-open` (and `aria-expanded`), Escape or a click outside closes it again. |

## Decisions worth writing down

* **The icon lives inside the heading, not beside it in a flex row.** A first attempt put
  `<h1>` and the icon in a `display: flex` wrapper: at 1200px it looked perfect, but as
  soon as the title wrapped (a ~390px phone) the heading took the whole line and the icon
  was pushed onto a line of its own, far from the words it belongs to. As an *inline-flex
  box in the text flow* the `?` simply follows the last word - `PDFs ?` - at every width,
  and the heading still wraps normally.
* **The hidden text is still in the page.** It stays in the DOM (only `opacity` and
  `visibility` change), so `tests/test_validation.py` still asserts
  *"review them before anything is saved"* is in the body, screen readers still get it
  through `aria-describedby`, and Ctrl+F still finds it on the page.
* **The popup is anchored to the icon only while that is safe.** A 26rem popup hanging off
  the icon runs past the right edge of the window as soon as the viewport is narrower than
  the icon's position plus 26rem - measured at 800px wide, the icon sits at x=417..440, so
  the popup would have spanned 409..825 inside a 769px-wide document: a horizontal
  scrollbar while hovering. The layout only reaches its 1080px maximum at ~1160px, so below
  that the icon becomes `position: static` and the popup is anchored to the `.panel-head`
  wrapper (`left: 0`, `width: min(26rem, 100%)`), which cannot leave the panel; the caret is
  dropped there, as it would point at empty space. Checked in headless Chrome at
  1400/1200/1160/1000/800/700 px and at a real 390px phone viewport (the page was rendered
  inside a 390px iframe, since Chrome enforces a ~500px minimum window):
  `documentElement.scrollWidth === clientWidth` at every one of them, with the popup's box
  inside the viewport.
* **`<span>` inside `<h1>`, `<span>` for the popup, no `<p>`.** A `<p>` inside a heading
  would be invalid; the popup's wording is short enough that phrasing content (`<strong>`
  included) says everything it needs to.
* **Hover is not the only way in.** `:focus-within` opens it for keyboard users (the
  button is focusable natively and gets the same 2px accent focus ring the field inputs
  use), `initHelpTips()` opens it on a tap, Escape or a click outside closes it again, and
  the button carries an `aria-label` of *What happens when you upload and how your files
  are handled* so the `?` is not a mystery to a screen reader. Being inside the `<h1>`
  means that label also becomes part of the heading's name in some screen readers - the
  trade-off for having the icon follow the title text at every width.
