// The case bar: "Copy my link" and the photo-only link to send to someone else. Both links are
// on the page as plain text too, so they can be selected and copied without this script.
(function () {
  function each(selector, run) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), run);
  }
  // Show each link with the address this browser used: behind a proxy the server can get the
  // scheme or the host wrong.
  each("[data-link]", function (el) {
    el.textContent = new URL(el.getAttribute("data-link"), window.location.href).href;
  });
  function select(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  each("[data-copy]", function (button) {
    var label = button.textContent;
    button.addEventListener("click", function () {
      var el = document.getElementById(button.getAttribute("data-copy"));
      function copied() {
        button.textContent = button.getAttribute("data-copied") || label;
        window.setTimeout(function () { button.textContent = label; }, 4000);
      }
      // No clipboard (an old browser, or a page not served over https): leave the link
      // selected so it can be copied by hand.
      function byHand() {
        select(el);
        try { if (document.execCommand("copy")) { copied(); } } catch (error) { /* selected */ }
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(el.textContent).then(copied, byHand);
      } else {
        byHand();
      }
    });
  });
  // The phone's own share sheet, where there is one.
  each("[data-share]", function (button) {
    if (!navigator.share) { return; }
    button.hidden = false;
    button.addEventListener("click", function () {
      var el = document.getElementById(button.getAttribute("data-share"));
      navigator.share({ title: "Waive", url: el.textContent }).catch(function () {});
    });
  });
})();
