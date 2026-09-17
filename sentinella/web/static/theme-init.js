/* Applies the saved theme synchronously, before the page is painted, so a
   light-theme user never sees a dark flash.

   This runs from the top of <body> (not <head>) so document.body already
   exists, and lives in its own file rather than an inline <script> so the
   page can be served under a CSP with no 'unsafe-inline'. */
(() => {
    "use strict";
    try {
        if (localStorage.getItem("theme") === "light") {
            document.body.classList.add("light-theme");
        }
    } catch (e) {
        /* storage blocked or unavailable — keep the default dark theme */
    }
})();
