/* ClearMark client-side validation. Mirrors the server limits in
   clearmark/validate.py for fast feedback. The server re-checks
   everything, so these messages are a convenience, not a guarantee. */

(function (global) {
  "use strict";

  var LIMITS = {
    image: { maxBytes: 50 * 1024 * 1024, maxLabel: "50 MB", maxMegapixels: 50 },
    video: { maxBytes: 100 * 1024 * 1024, maxLabel: "100 MB", maxSeconds: 30 },
    audio: { maxBytes: 30 * 1024 * 1024, maxLabel: "30 MB", maxSeconds: 300 }
  };
  var MAX_BATCH_FILES = 10;
  var MAX_BATCH_BYTES = 200 * 1024 * 1024;

  var IMAGE_EXTS = ["jpg", "jpeg", "png", "webp"];
  var VIDEO_EXTS = ["mp4", "mov", "m4v"];
  var AUDIO_EXTS = ["mp3", "wav", "m4a"];

  function extensionOf(name) {
    var parts = (name || "").toLowerCase().split(".");
    return parts.length > 1 ? parts[parts.length - 1] : "";
  }

  function classifyFile(file) {
    var name = (file.name || "").toLowerCase();
    if (name.endsWith(".gif")) {
      return { error: "Animated images (GIF) are not supported — cleaning would break the animation." };
    }
    var ext = extensionOf(file.name);
    if (IMAGE_EXTS.indexOf(ext) !== -1) { return { media: "image" }; }
    if (VIDEO_EXTS.indexOf(ext) !== -1) { return { media: "video" }; }
    if (AUDIO_EXTS.indexOf(ext) !== -1) { return { media: "audio" }; }
    return { error: "That format is not supported. Use JPEG, PNG or WebP images, MP4, MOV or M4V video, or MP3, WAV or M4A audio." };
  }

  function checkFile(file) {
    var classified = classifyFile(file);
    if (classified.error) { return classified.error; }
    var limit = LIMITS[classified.media];
    if (file.size > limit.maxBytes) {
      var label = classified.media === "image" ? "Images" : classified.media === "video" ? "Videos" : "Audio files";
      return label + " must be under " + limit.maxLabel + ".";
    }
    return null;
  }

  function checkBatch(files) {
    var list = Array.prototype.slice.call(files);
    if (list.length > MAX_BATCH_FILES) {
      return "Up to " + MAX_BATCH_FILES + " images can be processed at once.";
    }
    var total = list.reduce(function (sum, file) { return sum + file.size; }, 0);
    if (total > MAX_BATCH_BYTES) {
      return "A batch must be under 200 MB in total.";
    }
    var hasVideoOrAudio = list.some(function (file) {
      var classified = classifyFile(file);
      return classified.media === "video" || classified.media === "audio";
    });
    if (hasVideoOrAudio && list.length > 1) {
      return "Video and audio files are cleaned one at a time — select a single file.";
    }
    return null;
  }

  /* Duration check in the browser, best effort. Video/audio elements
     read the metadata without uploading anything. */
  function getDuration(file) {
    return new Promise(function (resolve) {
      var classified = classifyFile(file);
      if (!classified.media || classified.media === "image") { resolve(null); return; }
      var url = URL.createObjectURL(file);
      var el = document.createElement(classified.media === "video" ? "video" : "audio");
      var done = function (value) {
        URL.revokeObjectURL(url);
        resolve(value);
      };
      el.preload = "metadata";
      el.onloadedmetadata = function () {
        done(isFinite(el.duration) ? el.duration : null);
      };
      el.onerror = function () { done(null); };
      el.src = url;
      setTimeout(function () { done(null); }, 8000);
    });
  }

  function formatBytes(size) {
    if (size >= 1024 * 1024) { return (size / (1024 * 1024)).toFixed(1) + " MB"; }
    if (size >= 1024) { return Math.round(size / 1024) + " KB"; }
    return size + " B";
  }

  global.ClearMarkValidate = {
    LIMITS: LIMITS,
    classifyFile: classifyFile,
    checkFile: checkFile,
    checkBatch: checkBatch,
    getDuration: getDuration,
    formatBytes: formatBytes
  };
})(window);
