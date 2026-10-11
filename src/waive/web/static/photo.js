// Photo forms: one tap on the big button opens the camera; once a photo is chosen the form
// sends itself and the page says it is reading (the reader can take a while).
(function () {
  var forms = document.querySelectorAll("form[data-photo-form]");
  function show(form, waiting) {
    var button = form.querySelector("[data-photo-button]");
    var wait = form.querySelector("[data-photo-wait]");
    if (button) { button.hidden = waiting; }
    if (wait) { wait.hidden = !waiting; }
  }
  Array.prototype.forEach.call(forms, function (form) {
    var input = form.querySelector("input[type=file]");
    if (!input) { return; }
    input.addEventListener("change", function () {
      if (!input.files || !input.files.length) { return; }
      show(form, true);
      form.submit();
    });
  });
  // Coming back with the Back button: offer the button again.
  window.addEventListener("pageshow", function () {
    Array.prototype.forEach.call(forms, function (form) { show(form, false); });
  });
})();
