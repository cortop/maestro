/* Dependency page: hover tracing, search, keyboard walk, drag-pan and zoom. Vanilla; the page works without it. */
(function () {
  "use strict";

  var ZOOMS = [50, 65, 80, 100, 125, 150];
  var STATE = /(^|\s)(is-sel|is-focus|is-match|is-dim|lit-up|lit-down)(?=\s|$)/g;
  var zoom = 100;
  var search = "";
  var scroll = null;       // {left, top} saved just before the main region is swapped
  var hoverId = null;
  var focusId = null;
  var bound = new WeakSet();
  var serverClass = new WeakMap();
  var edgeOrder = [];

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function frame() { return $(".dep-scroll"); }
  function stage() { return $(".dep-stage"); }
  function nodes() { return $$("[data-id]", stage() || document.createElement("div")); }
  function traceable(el) { return el.classList.contains("dep-node") || el.classList.contains("dep-ghost"); }
  function depsOf(el) { return (el.getAttribute("data-deps") || "").split(" ").filter(Boolean); }

  /* ---- classes: the server's, then search, then the hovered trace on top ---- */

  function setState(el, token) {
    var base = serverClass.has(el) ? serverClass.get(el) : el.getAttribute("class") || "";
    if (!serverClass.has(el)) serverClass.set(el, base);
    var cls = token === null ? base : base.replace(STATE, "$1").replace(/\s+/g, " ").trim() + (token ? " " + token : "");
    if (el.getAttribute("class") !== cls) el.setAttribute("class", cls);
  }

  function trace(id) {
    var els = nodes();
    var deps = {}, rev = {};
    els.forEach(function (el) {
      var i = el.getAttribute("data-id");
      deps[i] = depsOf(el);
      deps[i].forEach(function (d) { (rev[d] = rev[d] || []).push(i); });
    });
    function reach(start, map, deep) {
      var seen = {}, todo = (map[start] || []).slice();
      while (todo.length) {
        var n = todo.pop();
        if (seen[n]) continue;
        seen[n] = true;
        if (deep) todo = todo.concat(map[n] || []);
      }
      return seen;
    }
    var done = id.indexOf("done:") === 0;
    var up = reach(id, deps, true);
    var down = reach(id, rev, !done);
    if (done) {
      Object.keys(down).forEach(function (k) {
        var el = els.filter(function (e) { return e.getAttribute("data-id") === k; })[0];
        if (el && el.classList.contains("dep-stack")) delete down[k];
      });
    }
    return { up: up, down: down };
  }

  function paint() {
    var active = hoverId || focusId;
    var els = nodes();
    var edges = $$(".dep-edge", stage() || document.createElement("div"));
    var lit = [];
    if (active && els.some(function (e) { return e.getAttribute("data-id") === active; })) {
      var t = trace(active);
      els.forEach(function (el) {
        var i = el.getAttribute("data-id");
        if (i === active) setState(el, el.classList.contains("dep-node") ? "is-focus" : "is-sel");
        else setState(el, t.up[i] || t.down[i] ? "" : "is-dim");
      });
      edges.forEach(function (g) {
        var s = g.getAttribute("data-s"), d = g.getAttribute("data-t"), tok;
        if ((t.up[d] || d === active) && t.up[s]) tok = "lit-up";
        else if ((t.down[s] || s === active) && t.down[d]) tok = "lit-down";
        else tok = "is-dim";
        setState(g, tok);
        if (tok !== "is-dim") lit.push(g);
      });
      lit.forEach(function (g) { g.parentNode.appendChild(g); });
      return;
    }
    edges.forEach(function (g) { setState(g, null); });
    edgeOrder.forEach(function (g) { if (g.parentNode) g.parentNode.appendChild(g); });
    var q = search.trim().toLowerCase();
    els.forEach(function (el) {
      if (!q) { setState(el, null); return; }
      if (el.classList.contains("dep-ghost")) { setState(el, "is-dim"); return; }
      setState(el, matches(el, q) ? "is-match" : "is-dim");
    });
    $$(".dep-unlinked").forEach(function (el) {
      setState(el, !q ? null : matches(el, q) ? "is-match" : "is-dim");
    });
  }

  function text(el) {
    var key = el.getAttribute("data-key") || ($(".dep-key", el) || {}).textContent || "";
    var title = $(".dep-node-title", el) || $(".dep-unlinked-title", el);
    return (key + " " + (title ? title.textContent : "")).toLowerCase();
  }
  function matches(el, q) {
    if (el.classList.contains("dep-stack")) {
      return (el.getAttribute("data-find") || "").toLowerCase().indexOf(q) >= 0;
    }
    return text(el).indexOf(q) >= 0;
  }

  function firstMatch() {
    var q = search.trim().toLowerCase();
    if (!q) return null;
    var all = $$(".dep-node", stage() || document.createElement("div")).concat($$(".dep-unlinked"));
    return all.filter(function (el) { return text(el).indexOf(q) >= 0; })[0] || null;
  }

  /* ---- hover / focus ---- */

  function bindNode(el) {
    if (bound.has(el)) return;
    bound.add(el);
    if (!el.hasAttribute("data-deps") || !traceable(el)) return;
    el.addEventListener("mouseenter", function () { hoverId = el.getAttribute("data-id"); paint(); });
    el.addEventListener("mouseleave", function () { hoverId = null; paint(); });
    el.addEventListener("focus", function () { focusId = el.getAttribute("data-id"); paint(); });
    el.addEventListener("blur", function () { focusId = null; paint(); });
    el.addEventListener("keydown", function (ev) { walk(el, ev); });
  }

  /* ---- keyboard ---- */

  function goto(el) {
    if (!el) return;
    el.focus({ preventScroll: true });
    var f = frame();
    if (!f) return;
    var fr = f.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (r.left < fr.left) f.scrollLeft -= fr.left - r.left + 16;
    else if (r.right > fr.right) f.scrollLeft += r.right - fr.right + 16;
    if (r.top < fr.top) f.scrollTop -= fr.top - r.top + 16;
    else if (r.bottom > fr.bottom) f.scrollTop += r.bottom - fr.bottom + 16;
  }

  function walk(el, ev) {
    var id = el.getAttribute("data-id");
    var cards = $$(".dep-node", stage());
    var next = null;
    if (ev.key === "Escape") {
      var x = $(".dep-x");
      if (x) x.click();
      ev.preventDefault();
      return;
    }
    if (ev.key === "ArrowRight") {
      next = cards.filter(function (c) { return depsOf(c).indexOf(id) >= 0; })[0];
    } else if (ev.key === "ArrowLeft") {
      var mine = depsOf(el);
      next = cards.filter(function (c) { return mine.indexOf(c.getAttribute("data-id")) >= 0; })[0];
    } else if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
      var lane = el.getAttribute("data-lane");
      var col = cards.filter(function (c) { return lane !== null && c.getAttribute("data-lane") === lane; })
        .sort(function (a, b) { return +a.getAttribute("data-row") - +b.getAttribute("data-row"); });
      var at = col.indexOf(el);
      next = col[at + (ev.key === "ArrowUp" ? -1 : 1)];
    } else {
      return;
    }
    ev.preventDefault();
    goto(next);
  }

  /* ---- pan ---- */

  function bindPan(f) {
    f.addEventListener("mousedown", function (ev) {
      if (ev.button !== 0 || ev.target.closest("[data-id]")) return;
      var x = ev.clientX, y = ev.clientY, l = f.scrollLeft, t = f.scrollTop, moved = false;
      function move(e) {
        if (!moved && Math.abs(e.clientX - x) + Math.abs(e.clientY - y) < 4) return;
        moved = true;
        f.scrollLeft = l - (e.clientX - x);
        f.scrollTop = t - (e.clientY - y);
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        if (moved) {
          var eat = function (e) { e.stopPropagation(); e.preventDefault(); };
          f.addEventListener("click", eat, { capture: true, once: true });
          setTimeout(function () { f.removeEventListener("click", eat, true); }, 0);
        }
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      ev.preventDefault();
    });
  }

  /* ---- zoom ---- */

  function applyZoom() {
    var sizer = $(".dep-sizer"), canvas = $(".dep-canvas");
    if (sizer && canvas) {
      if (!sizer.dataset.w) { sizer.dataset.w = parseFloat(sizer.style.width); sizer.dataset.h = parseFloat(sizer.style.height); }
      canvas.style.transform = zoom === 100 ? "" : "scale(" + zoom / 100 + ")";
      sizer.style.width = (sizer.dataset.w * zoom / 100) + "px";
      sizer.style.height = (sizer.dataset.h * zoom / 100) + "px";
    }
    $$("[data-dep-zoom=reset]").forEach(function (b) { b.textContent = zoom + "%"; });
  }

  function stepZoom(how) {
    var at = ZOOMS.indexOf(zoom);
    var to = how === "reset" ? 100 : ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, at + (how === "in" ? 1 : -1)))];
    var f = frame();
    var cx = 0, cy = 0;
    if (f) { cx = (f.scrollLeft + f.clientWidth / 2) / zoom; cy = (f.scrollTop + f.clientHeight / 2) / zoom; }
    zoom = to;
    applyZoom();
    if (f) { f.scrollLeft = cx * zoom - f.clientWidth / 2; f.scrollTop = cy * zoom - f.clientHeight / 2; }
  }

  /* ---- page wiring ---- */

  function init() {
    var s = stage();
    edgeOrder = s ? $$(".dep-edge", s) : [];
    edgeOrder.forEach(function (g) { if (!serverClass.has(g)) serverClass.set(g, g.getAttribute("class")); });
    nodes().forEach(bindNode);
    var f = frame();
    if (f && !bound.has(f)) { bound.add(f); bindPan(f); }
    $$("[data-dep-zoom]").forEach(function (b) {
      if (bound.has(b)) return;
      bound.add(b);
      b.addEventListener("click", function () { stepZoom(b.getAttribute("data-dep-zoom")); });
    });
    var input = $("[data-dep-find]");
    if (input && !bound.has(input)) {
      bound.add(input);
      input.addEventListener("input", function () { search = input.value; paint(); });
      input.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        var hit = firstMatch();
        if (hit) hit.click();
      });
    }
    if (input && input.value !== search) input.value = search;
    applyZoom();
    if (scroll && f) { f.scrollLeft = scroll.left; f.scrollTop = scroll.top; }
    scroll = null;
    hoverId = null;
    focusId = null;
    paint();
  }

  document.body.addEventListener("htmx:beforeSwap", function () {
    var f = frame();
    if (f) scroll = { left: f.scrollLeft, top: f.scrollTop };
  });
  document.body.addEventListener("htmx:afterSettle", function () { if ($("#deps-page")) init(); });
  init();
})();
