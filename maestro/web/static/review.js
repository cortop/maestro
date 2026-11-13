// Unsent review edits live in localStorage, keyed by ticket, PR head and finding; nothing is posted.
(function () {
  function root() { return document.getElementById("review"); }
  function storeKey(r, f) { return "maestro-review:" + r.dataset.key + ":" + r.dataset.head + ":" + f.dataset.index; }
  function read(el) { return el.type === "checkbox" ? el.checked : el.value; }
  function restore() {
    var r = root();
    if (!r) return;
    r.querySelectorAll(".finding").forEach(function (f) {
      var saved;
      try { saved = JSON.parse(localStorage.getItem(storeKey(r, f)) || "null"); } catch (e) { saved = null; }
      if (!saved) return;
      f.querySelectorAll("[data-field]").forEach(function (el) {
        var v = saved[el.dataset.field];
        if (v === undefined) return;
        if (el.type === "checkbox") el.checked = !!v; else el.value = v;
      });
    });
  }
  function save(e) {
    var r = root(), f = e.target.closest && e.target.closest(".finding");
    if (!r || !f) return;
    var state = {};
    f.querySelectorAll("[data-field]").forEach(function (el) { state[el.dataset.field] = read(el); });
    localStorage.setItem(storeKey(r, f), JSON.stringify(state));
  }
  document.addEventListener("input", save);
  document.addEventListener("change", save);
  document.addEventListener("htmx:afterSwap", restore);
  document.addEventListener("DOMContentLoaded", restore);
})();
