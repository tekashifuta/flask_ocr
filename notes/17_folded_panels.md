# The supporting panels fold away

The upload page and the database page both ended with tall, always-open cards of
reference text: *What happens to your file*, *Database storage (MySQL)*, the MySQL and
SQLite connection forms, *Connection status*, *Accepted input*. They are now accordions -
the heading is a row you click, the body is folded away until you do.

## How it works

The panel **is** the `<details>` and its heading is the `<summary>`, so this needs no
JavaScript at all: the browser implements the toggle, the keyboard handling (Tab to the
summary, Enter/Space to open, no ARIA and no click handler to get wrong) and the
`open`/closed state. `app.js` did not change.

```html
<details class="panel panel-muted accordion">
  <summary>
    <h2>What happens to your file</h2>
  </summary>
  <div class="accordion-body">
    <ol class="steps">…</ol>
    <dl class="facts">…</dl>
  </div>
</details>
```

## What changed, file by file

| File | What it does |
|---|---|
| `static/css/style.css` | A new *accordions* block: `.accordion` drops the panel padding (it moves to the header and the body, which is what makes the whole header line clickable), the chevron is two borders of a square, and `[open]` turns it around. |
| `templates/index.html` | The two asides became `<details>` panels; the database one carries a *Connected* / *Not connected* badge in its header so the status is still readable without opening it. |
| `templates/database.html` | The big first panel was split in two: a header panel (eyebrow, title, badge, *Records view*, lede and the **notices**) and a **MySQL server** accordion. The SQLite and Connection status panels became accordions too. |
| `templates/error.html` | *Accepted input* is the same accordion, for consistency. |
| `app/routes.py` | `_render_database_page(..., open_panel=...)` and its callers: the page unfolds the panel the request was about. |
| `tests/*` | New `test_the_side_panels_fold_away`, `test_the_store_panels_fold_away_and_reopen_for_the_store_that_was_used`, and the failed-connect test now also asserts the form comes back **unfolded**. |

## Decisions worth writing down

* **The result of a button is never folded away.** `POST /database/connect` passes
  `open_panel=<submitted backend>`, so a rejected MySQL form returns with the MySQL panel
  open and the values still in it; a connect that worked passes `open_panel="status"`, so
  the state that just changed is on screen. *Create missing tables* and *Forget saved
  details* do the same. The
  `.post("/database/connect")` test that used to omit `backend` now sends it, like the
  real form does (the hidden field is what identifies the panel).
* **The notices moved out of the folded panel.** `error` and `message` used to render
  inside the MySQL section; they now sit under the page lede in the header panel, where
  they cannot be hidden behind a collapsed summary - and the failed-connect message is
  therefore always visible.
* **The driver warning is the exception.** "The MySQL driver is not installed, so no MySQL
  connection can be made" is the one thing that still opens a panel by itself
  (`not database.driver.available`): a store that cannot work at all should say so before
  you click it.
* **What is allowed inside a `<summary>`.** Phrasing content, optionally mixed with
  heading content - so the heading is a real `<h2>` (it stays in the document outline) and
  the SQLite panel's *No server needed* eyebrow is a `<span class="eyebrow">`, not the
  `<p>` it was: a `<p>` is flow content and invalid there.
* **`flex-basis: 0` on the heading.** The header is a flex row of heading, badge and
  chevron. With the default basis the heading's *max-content* width wins and the badge and
  chevron wrap onto a line of their own on a phone; sizing the heading from the leftover
  space makes it wrap its own text instead, and the badge and chevron stay on the first
  line (checked at a real 390px viewport).
* **The panel look is unchanged when it is open.** The body keeps the same padding the
  panel used to have, so an unfolded panel is pixel-for-pixel the card it was before -
  only closed panels are shorter, which is the whole point.
