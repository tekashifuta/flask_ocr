# The "Connected" badge was stretched to the height of the button next to it

On the **Database storage** page (`/database`), the `MySQL storage` header showed
**Connected** as a tall green capsule with the word pinned to its top, while the same
badge on the `Use a local SQLite file` panel below rendered as a small, tidy pill. The
records view (`/database/records`) had the identical stretched badge next to
**Database setup**.

## Diagnosis (what was actually wrong)

* Both badges are one `<span class="badge badge-embedded">Connected</span>`; only their
  *container* differs, and that is where the layout was lost:

  ```html
  <!-- database.html / records.html: badge inside .result-actions -->
  <div class="result-actions">
    <span class="badge badge-embedded">Connected</span>
    <a class="button button-ghost">Records view</a>
  </div>
  ```

  ```css
  /* before */
  .result-actions { display: flex; flex-wrap: wrap; gap: .5rem; }  /* align-items: stretch */
  ```

* A flex row stretches its items on the cross axis by default. The badge has no height
  of its own, so it was blown up to the **button's** box (~2.7rem) - a `border-radius:
  999px` pill that tall reads as a smudge, and its single line of text sat at the top
  because the text is not vertically centred inside it.
* The SQLite panel does not use that wrapper (the badge is a direct child of
  `.result-head`, which sets `align-items: flex-start`), so the very same badge looked
  correct there - which is what made the header one look broken rather than stylised.
* Reproduced in a headless Chrome render of the real markup *before* the fix: the
  `Connected` pill was ~42px tall against the ~32px `Records view` button, text on the
  first line; the SQLite panel's pill stayed ~19px.

## What changed - `app/static/css/style.css` (CSS only, no markup or Python)

* `.result-actions` gained **`align-items: center`**, so a row holding a badge *and* a
  button lines both up on their own heights (this also covers `Not connected`, which is
  rendered by the same wrapper).
* `.badge` gained **`align-self: center`** as a belt-and-braces rule: a badge is a one
  line pill and must never be stretched by whatever flex row it happens to sit in - now
  even a future `result-actions`-like container that forgets `align-items` is safe.
* Both rules are inert where they are not needed: `.page-meta` (result/record pages)
  already centres its badges, and the `max-width: 640px` block that makes buttons
  `flex: 1 1 auto` is unaffected (the badge keeps its own width and is centred).

## Verified

* `env\Scripts\python.exe -m pytest -q` -> **221 passed** (unchanged suite: the fix is
  presentational, so no test asserted the broken layout - and none asserted against it).
* Headless Chrome screenshots of the pages the Flask test client renders
  (`/database` connected, `/database` idle, `/database/records`), light and dark mode:
  `Connected` / `Not connected` are now compact pills, vertically centred against
  **Records view** / **Database setup**, at 1000px and at 430px (the narrow layout puts
  the badge on its own line, still a pill).
* The SQLite panel's badge is unchanged (it was already correct), so both headers now
  read the same way.
