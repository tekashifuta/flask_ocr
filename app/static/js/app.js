/* Flask OCR - progressive enhancement only.
   Without JavaScript the form still works: it posts the selected file and the
   server validates everything again. */
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    initDropzone();
    initCopyButtons();
    initSubmitSpinner();
    initReviewForm();
    initDatabaseForm();
    initHelpTips();
  });

  /* ---------- drag & drop + client side pre-validation ---------- */
  function initDropzone() {
    var dropzone = document.getElementById("dropzone");
    var input = document.getElementById("file-input");
    if (!dropzone || !input) return;

    var filenameLabel = document.getElementById("dropzone-file");
    var fileList = document.getElementById("dropzone-list");
    var errorBox = document.getElementById("client-error");
    var form = dropzone.closest("form");
    var allowed = (form.dataset.allowed || "").toLowerCase();
    var maxBytes = parseInt(form.dataset.maxBytes || "0", 10);
    var maxFiles = parseInt(form.dataset.maxFiles || "0", 10);

    function showError(message) {
      if (!errorBox) return;
      errorBox.textContent = message;
      errorBox.hidden = !message;
    }

    function extensionOf(name) {
      var dot = name.lastIndexOf(".");
      return dot === -1 ? "" : name.slice(dot).toLowerCase();
    }

    function validate(file) {
      var ext = extensionOf(file.name);
      if (allowed.indexOf(ext.replace(".", "")) === -1) {
        showError("'" + file.name + "' is not supported. Allowed types: " + allowed.toUpperCase() + ".");
        return false;
      }
      if (maxBytes && file.size > maxBytes) {
        showError("'" + file.name + "' is too large. The limit is " + humanSize(maxBytes) + ".");
        return false;
      }
      showError("");
      return true;
    }

    function humanSize(bytes) {
      var units = ["B", "KB", "MB", "GB"];
      var value = bytes;
      var unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
      }
      return (unit === 0 ? value : value.toFixed(1)) + " " + units[unit];
    }

    /* One file or a whole batch: the server takes both, so does the page. */
    function describe(files) {
      var count = files ? files.length : 0;
      if (filenameLabel) {
        filenameLabel.textContent = count === 1
          ? "1 file selected"
          : count + " files selected";
        filenameLabel.hidden = count === 0;
      }
      if (!fileList) return;
      fileList.innerHTML = "";
      for (var index = 0; index < count; index += 1) {
        var item = document.createElement("li");
        item.textContent = files[index].name + " (" + humanSize(files[index].size) + ")";
        fileList.appendChild(item);
      }
      fileList.hidden = count === 0;
    }

    function checkAll(files) {
      if (!files || !files.length) {
        showError("");
        return true;
      }
      if (maxFiles && files.length > maxFiles) {
        showError(files.length + " files selected - one upload accepts at most " + maxFiles + ".");
        return false;
      }
      for (var index = 0; index < files.length; index += 1) {
        if (!validate(files[index])) return false;
      }
      return true;
    }

    dropzone.addEventListener("click", function () { input.click(); });
    dropzone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        input.click();
      }
    });

    input.addEventListener("change", function () {
      var files = input.files;
      describe(files);
      checkAll(files);
    });

    ["dragenter", "dragover"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    });

    ["dragleave", "dragend"].forEach(function (name) {
      dropzone.addEventListener(name, function () {
        dropzone.classList.remove("is-dragging");
      });
    });

    dropzone.addEventListener("drop", function (event) {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      if (!checkAll(files)) {
        describe(null);
        return;
      }
      input.files = files;
      describe(files);
    });
  }


  /* ---------- copy buttons ---------- */
  function initCopyButtons() {
    var feedback = document.getElementById("copy-feedback");
    var timer = null;

    function flash(message) {
      if (!feedback) return;
      feedback.textContent = message;
      feedback.hidden = false;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(function () { feedback.hidden = true; }, 2200);
    }

    function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
      }
      return new Promise(function (resolve, reject) {
        var helper = document.createElement("textarea");
        helper.value = text;
        helper.setAttribute("readonly", "");
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        document.body.appendChild(helper);
        helper.select();
        try {
          document.execCommand("copy") ? resolve() : reject(new Error("copy rejected"));
        } catch (error) {
          reject(error);
        } finally {
          document.body.removeChild(helper);
        }
      });
    }

    document.querySelectorAll("[data-copy-target]").forEach(function (button) {
      button.addEventListener("click", function () {
        var source = document.getElementById(button.dataset.copyTarget);
        if (!source) return;
        var text = source.tagName === "TEXTAREA" ? source.value : source.textContent;
        copyText(text).then(
          function () { flash("Copied to the clipboard."); },
          function () { flash("Copying failed - select the text manually."); }
        );
      });
    });
  }

  /* ---------- submit feedback ---------- */
  function initSubmitSpinner() {
    var form = document.querySelector(".upload-form");
    var button = document.getElementById("submit-button");
    if (!form || !button) return;

    form.addEventListener("submit", function () {
      if (button.disabled) return;
      var spinner = button.querySelector(".spinner");
      var label = button.querySelector(".button-label");
      button.disabled = true;
      if (spinner) spinner.hidden = false;
      if (label) label.textContent = "Extracting and reviewing...";
    });
  }

  /* ---------- review form ---------- */
  /* Marks a field whose value no longer matches what OCR proposed, and shows the
     spinner while the reviewed rows are stored.  The server validates every value
     again, so nothing here is load bearing. */
  function initReviewForm() {
    var form = document.getElementById("review-form");
    if (!form) return;

    form.querySelectorAll(".field input[type='text']").forEach(function (input) {
      var badge = input.parentNode.querySelector(".badge");
      if (!badge) return;
      input.addEventListener("input", function () {
        var corrected = input.value.trim() !== (input.defaultValue || "").trim();
        if (corrected) {
          badge.textContent = "corrected";
          badge.className = "badge badge-reviewed";
        } else {
          badge.textContent = badge.dataset.original || "as read";
          badge.className = "badge " + (badge.dataset.quality || "badge-missing");
        }
        input.parentNode.classList.remove("is-invalid");
      });
    });

    var button = document.getElementById("review-submit");
    if (!button) return;
    form.addEventListener("submit", function () {
      if (button.disabled) return;
      var spinner = button.querySelector(".spinner");
      var label = button.querySelector(".button-label");
      button.disabled = true;
      if (spinner) spinner.hidden = false;
      if (label) label.textContent = "Saving the reviewed data...";
    });
  }


  /* ---------- MySQL connection form ---------- */
  /* The form works without JavaScript too: it posts to /database/connect and the
     server renders the same page. With fetch we can report the result inline and
     reload only once the connection is really up. */
  function initDatabaseForm() {
    var form = document.getElementById("database-form");
    var status = document.getElementById("database-form-status");
    if (!form || !status || !window.fetch) return;

    var button = document.getElementById("database-submit");
    var spinner = button ? button.querySelector(".spinner") : null;
    var label = button ? button.querySelector(".button-label") : null;

    function field(id) {
      var input = document.getElementById(id);
      return input ? input.value : "";
    }

    function show(message, isError) {
      status.textContent = message;
      status.classList.toggle("is-error", !!isError);
      status.hidden = false;
    }

    function busy(isBusy) {
      if (!button) return;
      button.disabled = isBusy;
      if (spinner) spinner.hidden = !isBusy;
      if (label) label.textContent = isBusy ? "Connecting..." : "Connect & create schema";
    }

    function errorMessage(body) {
      if (body && body.error && body.error.message) return body.error.message;
      return "The MySQL connection could not be created.";
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var remember = document.getElementById("db-remember");
      var payload = {
        host: field("db-host"),
        port: field("db-port"),
        user: field("db-user"),
        password: field("db-password"),
        database: field("db-database"),
        table: field("db-table"),
        pages_table: field("db-pages-table"),
        charset: field("db-charset"),
        connect_timeout: field("db-timeout"),
        remember: !!(remember && remember.checked)
      };

      busy(true);
      show("Connecting to " + payload.host + ":" + payload.port + "...", false);

      fetch("/api/database/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (response) {
          return response.text().then(function (text) {
            var body = {};
            try {
              body = text ? JSON.parse(text) : {};
            } catch (error) {
              body = {};
            }
            return { ok: response.ok, body: body };
          });
        })
        .then(function (result) {
          if (!result.ok) {
            busy(false);
            show(errorMessage(result.body), true);
            return;
          }
          var database = result.body.database || {};
          show(database.message || "Connected - the schema is ready.", false);
          window.setTimeout(function () { window.location.reload(); }, 900);
        })
        .catch(function () {
          busy(false);
          show("The request failed - check that the server is still running.", true);
        });
    });
  }

  /* ---------- the question mark beside a title ---------- */
  /* Hover and keyboard focus open the popup through CSS (`.help-tip:hover`,
     `:focus-within`); this adds the two things CSS cannot do on its own: a tap
     toggles it on a touch screen, and Escape or a click elsewhere closes it. */
  function initHelpTips() {
    var tips = document.querySelectorAll(".help-tip");
    if (!tips.length) return;

    function close(tip) {
      tip.classList.remove("is-open");
      var button = tip.querySelector(".help-tip-button");
      if (button) button.setAttribute("aria-expanded", "false");
    }

    function closeAll() {
      document.querySelectorAll(".help-tip.is-open").forEach(close);
    }

    tips.forEach(function (tip) {
      var button = tip.querySelector(".help-tip-button");
      if (!button) return;

      button.addEventListener("click", function () {
        var open = !tip.classList.contains("is-open");
        closeAll();
        tip.classList.toggle("is-open", open);
        button.setAttribute("aria-expanded", open ? "true" : "false");
      });

      /* The pointer left the icon and its popup: nothing should stay open. */
      tip.addEventListener("mouseleave", function () { close(tip); });
      tip.addEventListener("focusout", function () {
        window.setTimeout(function () {
          if (!tip.contains(document.activeElement)) close(tip);
        }, 0);
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeAll();
    });

    document.addEventListener("click", function (event) {
      if (event.target instanceof Element && event.target.closest(".help-tip")) return;
      closeAll();
    });
  }
})();
