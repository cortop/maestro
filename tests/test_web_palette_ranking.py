"""Web palette ranking: the shipped palette_rank.js / palette.js run under node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "maestro" / "web" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

# A tiny fake DOM, just enough for palette.js: one flat <ul> of group/entry <li>s.
_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [dir, spec] = process.argv.slice(1);
const cfg = JSON.parse(spec);
let list;
class El {
  constructor(cls, ds) { this.cls = cls; this.dataset = Object.assign({}, ds); this.hidden = false; this.attrs = {}; }
  get classList() { const s = this; return {
    contains: c => s.cls.includes(c), add: c => s.cls.push(c),
    remove: c => { s.cls = s.cls.filter(x => x !== c); } }; }
  removeAttribute(a) { delete this.attrs[a]; } setAttribute(a, v) { this.attrs[a] = v; }
  scrollIntoView() {}
  after(n) { const l = this.parent.kids; l.splice(l.indexOf(n), 1); l.splice(l.indexOf(this) + 1, 0, n); }
}
const sel = s => s.split(",").map(x => x.trim().slice(1));
list = { kids: [], addEventListener() {},
  querySelectorAll(s) { const cs = sel(s); return this.kids.filter(k => s.split(",").some(p => p.trim().split(".").filter(Boolean).every(c => k.cls.includes(c)))); },
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }, innerHTML: "" };
const input = { value: "", addEventListener() {}, focus() {} };
function load(items) {
  list.kids = [];
  for (const it of items) {
    const e = it.group ? new El(["palette-group"]) : new El(["palette-entry"], { text: it.text });
    e.parent = list; list.kids.push(e); e.name = it.group || it.text;
  }
}
let filter;
const spy = { value: "", focus() {}, addEventListener(t, f) { if (t === "input") filter = f; } };
const dlg = { querySelector: s => s === "input" ? spy : (s === "ul" ? list : { textContent: "" }),
  setAttribute() {}, addEventListener() {}, set innerHTML(v) {} };
const kd = {};
const doc = { addEventListener(t, f) { kd[t] = f; }, querySelectorAll: () => [],
  body: { dataset: {}, getAttribute: () => null, appendChild() {} }, createElement: () => dlg };
const win = {};
const ctx = { window: win, document: doc, navigator: { platform: "" }, fetch: () => new Promise(() => {}) };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(dir + "/palette_rank.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync(dir + "/palette.js", "utf8"), ctx);
kd.keydown({ metaKey: true, key: "k", preventDefault() {} });
load(cfg.items);
const steps = [];
for (const q of cfg.queries) {
  spy.value = q; filter();
  steps.push({ q, visible: list.kids.filter(k => !k.hidden).map(k => k.name),
    selected: list.kids.filter(k => k.attrs["aria-selected"]).map(k => k.name) });
}
console.log(JSON.stringify(steps));
"""


def _run(items, queries):
    r = subprocess.run(
        ["node", "-e", _HARNESS, str(STATIC), json.dumps({"items": items, "queries": queries})],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return {s["q"]: s for s in json.loads(r.stdout)}


def _items():
    return [
        {"group": "Go to"},
        {"text": "t-20 other work"}, {"text": "t-274 more"}, {"text": "t-2 broken thing"},
        {"text": "xx t-2 mention"}, {"text": "retry t-2 later"},
        {"group": "Fleet"},
        {"text": "back up now"}, {"text": "pause fleet"}, {"text": "up"},
    ]


def test_scores_prefix_then_word_start_then_substring_ties_in_server_order():
    s = _run(
        [{"group": "G"}, {"text": "xxfoo"}, {"text": "a foo"}, {"text": "foo b"}, {"text": "bar foo"}, {"text": "xfoo"}],
        ["foo"],
    )["foo"]
    assert s["visible"] == ["G", "foo b", "a foo", "bar foo", "xxfoo", "xfoo"]


def test_ticket_key_query_puts_exact_ticket_first():
    s = _run(_items(), ["t-2"])["t-2"]
    assert s["visible"][:4] == ["Go to", "t-2 broken thing", "t-20 other work", "t-274 more"]


def test_selection_follows_first_visible_and_clearing_restores_server_order():
    steps = _run(_items(), ["t-2", "up", "", "zzz"])
    assert steps["t-2"]["selected"] == ["t-2 broken thing"]
    assert steps["up"]["visible"] == ["Fleet", "up", "back up now"]
    assert steps["up"]["selected"] == ["up"]
    assert steps[""]["visible"] == [i.get("group") or i["text"] for i in _items()]
    assert steps[""]["selected"] == ["t-20 other work"]
    assert steps["zzz"]["selected"] == []
