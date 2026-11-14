/* Header credit balance. Fetches the server-side balance and updates
   the pill in the header. Safe to call when logged out (hides itself). */

(function (global) {
  "use strict";

  function refreshCredits() {
    var pill = document.getElementById("header-credits");
    if (!pill) { return Promise.resolve(); }
    return fetch("/api/credits", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) { return null; }
        return response.json();
      })
      .then(function (data) {
        if (!data) { return; }
        pill.textContent = data.balance + " credits";
        if (!data.freeUsed) {
          pill.title = "Your first image removal is free";
        } else {
          pill.title = "Your credit balance";
        }
      })
      .catch(function () { /* balance stays as rendered by the server */ });
  }

  document.addEventListener("DOMContentLoaded", refreshCredits);
  global.refreshCredits = refreshCredits;
})(window);
