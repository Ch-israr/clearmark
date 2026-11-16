/* ClearMark tool: dropzone, upload with progress, job polling, results.
   The browser never processes a file — it uploads it, polls the job,
   and offers the cleaned download. */

(function () {
  "use strict";

  var POLL_IMAGE_MS = 2000;
  var POLL_VIDEO_MS = 10000;

  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var browseButton = document.getElementById("browse-button");
  var fileList = document.getElementById("file-list");
  var errorBox = document.getElementById("dropzone-error");
  var wakeNote = document.getElementById("wake-note");
  var recentBlock = document.getElementById("recent-block");
  var recentList = document.getElementById("recent-list");

  if (!dropzone) { return; }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function statusText(op) {
    if (op.status === "queued") { return "Queued"; }
    if (op.status === "running") { return "Cleaning…"; }
    if (op.status === "succeeded") { return "Cleaned"; }
    if (op.status === "failed") { return "Failed"; }
    return op.status;
  }

  function makeCard(file) {
    var card = document.createElement("article");
    card.className = "file-card";
    var head = document.createElement("div");
    head.className = "file-card-head";
    var name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name;
    var meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = window.ClearMarkValidate.formatBytes(file.size);
    var pill = document.createElement("span");
    pill.className = "status-pill";
    pill.textContent = "Uploading…";
    head.appendChild(name);
    head.appendChild(meta);
    head.appendChild(pill);
    var progress = document.createElement("div");
    progress.className = "progress";
    var bar = document.createElement("div");
    bar.className = "progress-bar";
    progress.appendChild(bar);
    var note = document.createElement("p");
    note.className = "file-note";
    card.appendChild(head);
    card.appendChild(progress);
    card.appendChild(note);
    fileList.prepend(card);
    return { card: card, pill: pill, bar: bar, progress: progress, note: note };
  }

  function renderDone(elements, op) {
    elements.progress.remove();
    elements.pill.textContent = statusText(op);
    elements.pill.classList.add(op.status);
    if (op.status === "succeeded") {
      var details = op.details || {};
      if (details.removed && details.removed.length) {
        var list = document.createElement("ul");
        list.className = "removed-list";
        details.removed.forEach(function (item) {
          var li = document.createElement("li");
          li.textContent = "Removed: " + item;
          list.appendChild(li);
        });
        elements.card.appendChild(list);
      }
      if (details.note) {
        elements.note.textContent = details.note;
      }
      var actions = document.createElement("div");
      actions.className = "file-actions";
      var link = document.createElement("a");
      link.className = "btn";
      link.href = op.resultUrl;
      link.textContent = "Download cleaned file";
      link.setAttribute("download", op.resultName || op.filename);
      actions.appendChild(link);
      var expiry = document.createElement("span");
      expiry.className = "expiry-note";
      expiry.textContent = "Download link expires 1 hour after upload.";
      actions.appendChild(expiry);
      elements.card.appendChild(actions);
    } else if (op.status === "failed") {
      var message = (op.error && op.error.message) || "Cleaning failed.";
      elements.note.textContent = message;
      elements.note.classList.add("error");
      if (op.fundingState === "refunded") {
        var refund = document.createElement("p");
        refund.className = "file-note";
        refund.textContent = "Your credits for this file were refunded.";
        elements.card.appendChild(refund);
      }
    }
    if (window.refreshCredits) { window.refreshCredits(); }
    loadRecent();
  }

  function pollOperation(opId, media, elements) {
    var interval = media === "video" ? POLL_VIDEO_MS : POLL_IMAGE_MS;
    var timer = setInterval(function () {
      fetch("/api/operations/" + encodeURIComponent(opId), { credentials: "same-origin" })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          var op = data.operation;
          if (!op) { clearInterval(timer); return; }
          elements.pill.textContent = statusText(op);
          if (op.status === "succeeded" || op.status === "failed") {
            clearInterval(timer);
            renderDone(elements, op);
          }
        })
        .catch(function () { /* keep polling; a transient error is not a failure */ });
    }, interval);
  }

  function startOperation(upload, elements, media) {
    var operationId = upload.operationId;
    fetch("/api/operations", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": operationId
      },
      body: JSON.stringify({ uploadId: upload.uploadId })
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var op = data.operation;
        if (!op) {
          elements.pill.textContent = "Failed";
          elements.note.textContent = "The server did not start the cleaning job.";
          elements.note.classList.add("error");
          return;
        }
        elements.bar.style.width = "100%";
        elements.pill.textContent = statusText(op);
        elements.pill.classList.add("running");
        pollOperation(op.id, media, elements);
      })
      .catch(function () {
        elements.pill.textContent = "Failed";
        elements.note.textContent = "Could not reach the server. Your credits were not charged.";
        elements.note.classList.add("error");
      });
  }

  function selectedMode() {
    var checked = document.querySelector("input[name='clean-mode']:checked");
    return checked && checked.value === "strong" ? "strong" : "standard";
  }

  function uploadFile(file, batchCount, batchTotal) {
    var elements = makeCard(file);
    var operationId = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : ("op-" + Date.now() + "-" + Math.random().toString(36).slice(2));
    var form = new FormData();
    form.append("file", file);
    form.append("operationId", operationId);
    form.append("mode", selectedMode());
    form.append("batchCount", String(batchCount));
    form.append("batchTotalBytes", String(batchTotal));
    var wakeTimer = setTimeout(function () { wakeNote.hidden = false; }, 5000);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads", true);
    xhr.withCredentials = true;
    xhr.upload.addEventListener("progress", function (event) {
      if (event.lengthComputable) {
        elements.bar.style.width = Math.round((event.loaded / event.total) * 100) + "%";
      }
    });
    xhr.addEventListener("load", function () {
      clearTimeout(wakeTimer);
      wakeNote.hidden = true;
      var data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = {}; }
      if (xhr.status === 401) {
        elements.pill.textContent = "Log in needed";
        elements.note.textContent = "";
        elements.note.textContent = "Log in or sign up to clean files — your first image removal is free.";
        elements.note.classList.add("error");
        return;
      }
      if (xhr.status !== 201 && xhr.status !== 200) {
        elements.pill.textContent = "Failed";
        elements.pill.classList.add("failed");
        elements.note.textContent = (data.error && data.error.message) || "Upload failed.";
        elements.note.classList.add("error");
        return;
      }
      var classified = window.ClearMarkValidate.classifyFile(file);
      startOperation(
        { uploadId: data.uploadId, operationId: data.operationId },
        elements,
        classified.media
      );
    });
    xhr.addEventListener("error", function () {
      clearTimeout(wakeTimer);
      wakeNote.hidden = true;
      elements.pill.textContent = "Failed";
      elements.note.textContent = "Could not reach the server. Check your connection and try again.";
      elements.note.classList.add("error");
    });
    xhr.send(form);
  }

  function handleFiles(fileListInput) {
    clearError();
    var files = Array.prototype.slice.call(fileListInput);
    if (!files.length) { return; }
    var batchError = window.ClearMarkValidate.checkBatch(files);
    if (batchError) { showError(batchError); return; }
    var checks = files.map(function (file) {
      return window.ClearMarkValidate.getDuration(file).then(function (seconds) {
        var error = window.ClearMarkValidate.checkFile(file);
        if (error) { return error; }
        var classified = window.ClearMarkValidate.classifyFile(file);
        if (classified.media === "video" && seconds !== null && seconds > 30) {
          return "Videos up to 30 seconds are supported.";
        }
        if (classified.media === "audio" && seconds !== null && seconds > 300) {
          return "Audio up to 300 seconds (5 minutes) is supported.";
        }
        return null;
      });
    });
    Promise.all(checks).then(function (errors) {
      var firstError = errors.filter(Boolean)[0];
      if (firstError) { showError(firstError); return; }
      var total = files.reduce(function (sum, file) { return sum + file.size; }, 0);
      files.forEach(function (file) { uploadFile(file, files.length, total); });
      fileInput.value = "";
    });
  }

  dropzone.addEventListener("click", function (event) {
    if (event.target === browseButton) { return; }
    fileInput.click();
  });
  browseButton.addEventListener("click", function (event) {
    event.stopPropagation();
    fileInput.click();
  });
  dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", function () { handleFiles(fileInput.files); });
  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.add("drag-over");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.remove("drag-over");
    });
  });
  dropzone.addEventListener("drop", function (event) {
    handleFiles(event.dataTransfer.files);
  });

  function loadRecent() {
    if (!recentList) { return; }
    // The credits pill only exists for logged-in users; logged-out
    // visitors have no recent list, so skip the request entirely
    // instead of hitting the API and logging a 401.
    if (!document.getElementById("header-credits")) { return; }
    fetch("/api/operations?limit=5", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) { return null; }
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.operations || !data.operations.length) { return; }
        recentBlock.hidden = false;
        recentList.replaceChildren();
        data.operations.forEach(function (op) {
          var li = document.createElement("li");
          var name = document.createElement("span");
          name.className = "file-name";
          name.textContent = op.filename;
          var meta = document.createElement("span");
          meta.className = "file-meta";
          meta.textContent = statusText(op) + " · " + op.cost + " credits";
          li.appendChild(name);
          if (op.status === "succeeded" && op.resultUrl) {
            var link = document.createElement("a");
            link.href = op.resultUrl;
            link.textContent = "Download";
            li.appendChild(link);
          }
          li.appendChild(meta);
          recentList.appendChild(li);
        });
      })
      .catch(function () { /* recent list is a nicety, not a blocker */ });
  }

  loadRecent();
})();
