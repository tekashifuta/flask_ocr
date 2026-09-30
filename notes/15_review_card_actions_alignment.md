# *Store this document* sat 8px lower than *Open result page* in a review card

On the review page (`/upload` -> review step) every document card carries a small
toolbar on the right of its heading: the **Open result page** link-button and, next
to it, the **Store this document** checkbox with its label. The checkbox and its
label hung visibly below the button - exactly the ragged row the screenshot in the
report shows.

## Diagnosis (what was actually wrong)

* The markup is a flex row of two items, and the second one is the *shared*
  `.checkbox` label:

  ```html
  <div class="review-card-actions">
    <a class="button button-tiny" ...>Open result page</a>
    <label class="checkbox checkbox-inline">
      <input type="checkbox" name="include_0" value="1" checked>
      <span>Store this document</span>
    </label>
  </div>
  ```

  ```css
  /* before */
  .checkbox { display: flex; align-items: flex-start; gap: .5rem; margin-top: 1rem; ... }
  ```

* `.review-card-actions` already had **`align-items: center`** - the toolbar was not
  the problem. The **`margin-top: 1rem`** on `.checkbox` was: that style exists for
  the *form* checkboxes (**Remember these details**, **Save the reviewed data to ...**)
  which need breathing room above them, but a flex item's own margin moves *the item*
  inside the row, so it pushed the whole checkbox-and-label group ~16px below the
  button. `checkbox-inline` was in the class list but **no rule defined it** - the
  modifier was written and then never styled.
* Measured in headless Chrome on the real markup *before* the fix (1400px wide):
  button box `top 103, height 28, centre 117`; label `top 114, height 22, centre 125`
  - the label row's centre was **8px lower** than the button's.
* A second, smaller offset was hidden inside the label: the UA stylesheet's
  `margin: 3px 3px 3px 4px` on `input[type=checkbox]` leaves a **bottom** margin, and
  `.checkbox input { margin-top: .18rem }` only patched the top one. The 13px box
  therefore landed at centre 123.4 while its own 22px label box centred on 124.9 -
  the tick box floated 1.5px above the text it belongs to.

## What changed - `app/static/css/style.css` (CSS only, no markup or Python)

* The already-present but unstyled modifier got its rules, next to `.checkbox`:

  ```css
  /* The review card toolbar shows the same checkbox next to a button: the top
     margin would push the whole row below the button, and the UA's bottom margin
     would lift the box 1.5px above its own label. */
  .checkbox-inline { margin-top: 0; align-items: center; }
  .checkbox-inline input { margin-top: 0; margin-bottom: 0; }
  ```

  `margin-top: 0` removes the offset, `margin-bottom: 0` cancels the UA margin so the
  13px control centres on the label's line, and `align-items: center` replaces the
  `.checkbox` default `flex-start` (which only makes sense for a label that wraps to
  several lines).
* Nothing else touching `.checkbox` changed, so the two *form* checkboxes and their
  `.checkbox input { margin-top: .18rem }` rule are untouched, and the HTML needed no
  edit - the hook was already there.

## Verified

* `env\Scripts\python.exe -m pytest -q` -> **301 passed** (the fix is presentational;
  no test asserted the broken layout).
* Headless Chrome geometry of the real page the Flask test client renders
  (`/upload` with two text-layer PDFs), *after* the fix:

  | viewport | button | checkbox label |
  |---|---|---|
  | 1400px | top 98, height 28, centre **112** | top 101, height 22, centre **112** |
  | 420px (wrapped onto its own line) | top 158, height 28, centre **172** | top 161, height 22, centre **172** |

  and the 13px checkbox itself reports centre 111.9, i.e. all three boxes share one
  centre line to the pixel.
* Screenshots of the same markup before/after, dark **and** forced-light, at 1400px
  and 420px: the row reads as one line in the fixed version, while the "before" copy
  reproduces the reported 8px drop.