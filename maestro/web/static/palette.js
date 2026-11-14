// Command palette: Cmd/Ctrl+K opens it; the entry list is a server-rendered fragment from /palette.
(function () {
  var dlg, input, list, tokenHeaders = {};
  function entries() { return Array.prototype.filter.call(list.querySelectorAll(".palette-entry"), function (e) { return !e.hidden; }); }
  function select(i) {
    var es = entries();
    list.querySelectorAll(".palette-entry.selected").forEach(function (e) { e.classList.remove("selected"); e.removeAttribute("aria-selected"); });
    if (!es.length) return;
    var e = es[(i + es.length) % es.length];
    e.classList.add("selected"); e.setAttribute("aria-selected", "true"); e.scrollIntoView({ block: "nearest" });
  }
  function current() { return entries().indexOf(list.querySelector(".palette-entry.selected")); }
  function filter() {
    var q = input.value, groups = [], cur = null;
    Array.prototype.forEach.call(list.querySelectorAll(".palette-group, .palette-entry"), function (n, i) {
      if (n.classList.contains("palette-group")) { cur = { head: n, items: [] }; groups.push(cur); return; }
      if (n.dataset.order === undefined) n.dataset.order = String(i);
      if (cur) cur.items.push(n);
    });
    groups.forEach(function (g) {
      var items = g.items.sort(function (a, b) { return a.dataset.order - b.dataset.order; });
      var order = window.PaletteRank.rank(items.map(function (e) { return e.dataset.text; }), q);
      var shown = order.map(function (i) { return items[i]; });
      var rest = items.filter(function (e) { return shown.indexOf(e) === -1; });
      var ptr = g.head;
      shown.forEach(function (e) { e.hidden = false; ptr.after(e); ptr = e; });
      rest.forEach(function (e) { e.hidden = true; ptr.after(e); ptr = e; });
      g.head.hidden = !shown.length;
    });
    select(0);
  }
  function close() { if (dlg) { dlg.hidden = true; } }
  function run(e) {
    if (!e) return;
    if (e.dataset.href) { window.location.href = e.dataset.href; return; }
    var h = {}, tok = document.body.getAttribute("hx-headers");
    if (tok) { try { h = JSON.parse(tok); } catch (x) {} }
    fetch(e.dataset.post, { method: "POST", headers: h }).then(function () { window.location.reload(); });
  }
  function open() {
    if (!dlg) build();
    dlg.hidden = false; input.value = "";
    var key = document.body.dataset.ticket || "";
    fetch("/palette" + (key ? "?key=" + encodeURIComponent(key) : "")).then(function (r) { return r.text(); }).then(function (html) {
      list.innerHTML = html; filter();
    });
    input.focus();
  }
  function build() {
    dlg = document.createElement("div"); dlg.id = "palette"; dlg.className = "palette"; dlg.hidden = true;
    dlg.setAttribute("role", "dialog"); dlg.setAttribute("aria-label", "Command palette");
    dlg.innerHTML = '<div class="palette-box"><div class="palette-inner">' +
      '<div class="palette-top"><span class="palette-context"></span>' +
      '<input type="text" class="palette-input" placeholder="Type a command, a ticket key or a title…" aria-label="Command">' +
      '<span class="palette-esc">ESC</span></div>' +
      '<ul class="palette-list" role="listbox"></ul>' +
      '<div class="palette-foot">↑↓ move · ↵ run · esc close</div></div></div>';
    dlg.querySelector(".palette-context").textContent = "> " + (document.body.dataset.ticket || "maestro");
    document.body.appendChild(dlg);
    input = dlg.querySelector("input"); list = dlg.querySelector("ul");
    input.addEventListener("input", filter);
    dlg.addEventListener("mousedown", function (ev) { if (ev.target === dlg) close(); });
    list.addEventListener("click", function (ev) { var e = ev.target.closest(".palette-entry"); if (e) run(e); });
  }
  document.addEventListener("keydown", function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); if (dlg && !dlg.hidden) close(); else open(); return; }
    if (!dlg || dlg.hidden) return;
    if (ev.key === "Escape") { ev.preventDefault(); close(); }
    else if (ev.key === "ArrowDown") { ev.preventDefault(); select(current() + 1); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); select(current() - 1); }
    else if (ev.key === "Enter") { ev.preventDefault(); run(entries()[current()]); }
  });
  document.addEventListener("click", function (ev) { if (ev.target.closest("[data-palette-open]")) open(); });
  if (/Mac|iPhone|iPad/.test(navigator.platform)) {
    document.querySelectorAll("[data-palette-key]").forEach(function (k) { k.textContent = "⌘K"; });
  }
})();
