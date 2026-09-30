# The storage page is one card, and its details hang off a question mark

`/database` had two cards saying the same thing on top of each other: a header panel
(eyebrow, `MySQL storage`, the *Connected* badge, *Records view*, a lede paragraph) and,
below it, a folded **MySQL server** panel whose own lede repeated the schema story before
the form. The header and the form are now **one card**, and both lede paragraphs are the
popup of a `?` at the lower right of its heading - exactly what note 18 did to the upload
page. Only the SQLite form and the connection status still fold away.

```html
<section class="panel">
  <div class="panel-head">
    <div class="panel-head-text">
      <p class="eyebrow">Database storage</p>
      <h1>MySQL storage</h1>
    </div>
    <div class="result-actions"> badge + Records view </div>
    <div class="help-tip"> ? + .help-tip-popup#storage-help </div>
  </div>
  notices …
  <h2 class="form-title">MySQL server</h2>
  <form class="db-form" id="database-form"> … </form>
</section>
```

## What changed, file by file

| File | What it does |
|---|---|
| `templates/database.html` | The header `</section>` and the MySQL `<details>/<summary>/<div class="accordion-body">` are gone: one `<section class="panel">` holds the `.panel-head`, the notices, a `<h2 class="form-title">MySQL server</h2>`, the driver warning and the form. Both old lede paragraphs moved into `.help-tip-popup#storage-help` (a `<code>` per schema/table name), and the `?` is the third item of the head. The form's children were dedented one level with it. |
| `static/css/style.css` | `.panel-head` may now wrap (`flex-wrap: wrap`), gained `.panel-head-text` (eyebrow above the heading as *one* item: `display: grid; flex: 1 1 14rem; min-width: 0`) and `.panel-head > .result-actions { align-self: flex-start }`. `.help-tip` gained `margin-left: auto`, and a new one-line `.form-title` rule spaces the form's heading when a notice sits above it. |
| `app/routes.py` | `_render_database_page()`'s docstring: `open_panel` is now `sqlite`/`status` only - the MySQL form has no folded panel to unfold. No signature or caller changed. |
| `tests/test_database.py` | `test_database_page_renders_the_connection_form` also pins `<h2 class="form-title">MySQL server</h2>` and `body.count("<details") == 2`; a new `test_the_storage_details_hang_off_the_question_mark` pins the popup to its `?` and both paragraphs *inside* it; the failed-connect test now asserts the form is on screen (`id="db-host"`) and that no `class="panel accordion"` exists at all. |
| `tests/test_sqlite.py` | `test_the_store_panels_fold_away_and_reopen_for_the_store_that_was_used` counts two `<details>` instead of three and asserts the MySQL form is never folded. |
| `static/js/app.js` | Untouched - `initHelpTips()` iterates every `.help-tip`, so a second icon needs no JavaScript. |

## Decisions worth writing down

* **The badge and the `?` share a row, and only one of them moves.** A badge or a button
  beside a heading belongs at the *top* of it (`align-items: flex-start` in `.result-head`,
  which is what the old header showed), while the `?` is a heading's *lower right* corner
  (note 18). So `.panel-head > .result-actions` pins itself to the top of the flex line and
  the icon keeps the bottom - and the `?` stays the last item on its line.
* **`margin-left: auto` on the icon is what keeps the caret pointing at it.** `.panel-head`
  can wrap now, and when it does, the icon shares a line with the badge and the button
  instead of sitting at the end of a full line. An auto margin absorbs the free space on
  *that* line, so the icon's right edge is the head's right edge again - the invariant note
  18 measured (`icon.right - head.right = 0`, `icon.bottom - head.bottom = 0`).
* **The text block is one item, not two.** Eyebrow and `h1` live in `.panel-head-text`
  (`display: grid`), so the head is a row of *three* things - text, actions, icon - and the
  eyebrow cannot end up on the actions' line. `flex: 1 1 14rem` gives it the leftover width
  *and* a floor: below ~430px of head width the badge and the button drop to a second line
  and the heading keeps a line of its own, rather than both being squeezed (the
  `@media (max-width: 640px)` rule already makes `.result-actions` full width there).
* **Measured in headless Chrome** on the real rendered page (a measuring script plus
  `--dump-dom`, and a 390px iframe because Chrome will not open a window that narrow): the
  icon is flush right *and* bottom-aligned at every width - 684px `615,136 .. 638,159`
  against `head=46,104 .. 638,159`, and at a real 390px `icon=332,250 .. 355,273` against
  `head=35,139 .. 355,273`, with the heading on its own line and the badge + button on the
  next. The upload page's own `?` did not move: still `332..355` against `head=35..355`.
* **The driver warning no longer needs to open a panel.** It used to be the exception note
  17 auto-unfolded ("a store that cannot work at all should say so before you click it");
  the card it lives in is never folded now, so the rule is satisfied without any condition.
* **`<h2 class="form-title">MySQL server</h2>` survives the merge on purpose.** The `h1` is
  the *configured* store (`SQLite storage` under `DATABASE_BACKEND=sqlite`), while the form
  below it is always the MySQL one, so the form still has to name itself - and "MySQL
  server" is the string the tests and the docs point at.
* **Nothing was trimmed, and the popup covers the form while it is open.** 30rem wide and
  ~540px tall (measured), it scrolls itself (`max-height: calc(100vh - 15rem)`) and opens
  over the fields - the same trade-off note 18 accepted on the upload page. The page is two
  paragraphs and one panel header shorter than before, so nothing is pushed off-screen.
* **One pre-existing bug is visible in those numbers and was left alone.** At a true 390px
  `/database` still scrolls sideways by ~180px, and it is *not* this card: hiding the form
  drops `document.scrollWidth` from 571 to 390, and `.checkbox { overflow-wrap: anywhere }`
  does the same on its own - it is the unbreakable `instance\mysql_connection.json` path in
  *Remember these details*. That markup is untouched by this change and the overflow depends
  on where `instance/` happens to live, so it is reported rather than fixed here.

