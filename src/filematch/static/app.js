// filematch: small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  // Ask before destructive actions (Clear session).
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // Disable the submit button while a long request (upload, compare) runs.
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]:not([formaction])");
      if (button) {
        button.disabled = true;
        button.textContent = form.getAttribute("data-busy");
      }
    });
  });

  // Mapping screen: mark rows the user changed or set to ignore.
  document.querySelectorAll("select[data-mapping]").forEach(function (select) {
    var initial = select.value;
    select.addEventListener("change", function () {
      var row = select.closest("tr");
      if (!row) return;
      row.classList.toggle("edited", select.value !== initial);
      row.classList.toggle("ignored", select.value === "");
    });
  });
})();
