"""QA review-skill Note contract: skill instructions and the rounds-view rendering."""
from __future__ import annotations

import json
import os
from pathlib import Path

from maestro import event_log, snapshot as snap_mod, store
from maestro.view import rounds as rounds_view

KEY = "T-901"
T0 = 1_700_000_000.0
SKILL = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "maestro-reconcile-qa.md"


def test_skill_instructs_single_structured_note():
    text = SKILL.read_text()
    for needle in ("exactly ONE Note", '"review_skill"', '"status"', "ran|skipped|error",
                   "empty for zero", "`skipped`", "`error`"):
        assert needle in text


def _seed(home, monkeypatch, note, log_records):
    t = [T0]
    monkeypatch.setattr(store, "iso_now", lambda: store.epoch_to_iso(t[0]))

    def add(typ, payload, after=0):
        t[0] += after
        event_log.append(home, KEY, typ, payload, actor="t")
        snap_mod.rebuild(home, KEY)

    store.atomic_write(store.spec_path(home, KEY), f"# {KEY}\n\n## Acceptance criteria\n- [ ] one\n")
    add("TicketCreated", {"title": "Synthetic", "source": "test", "spec_hash": "x"})
    add("PhaseChanged", {"phase": "qa", "reason": "implementing round done"}, 60)
    add("Note", note, 5)
    add("AcQaVerdict", {"ac_hash": "h1", "ac_index": 1, "verdict": "pass", "evidence": "ok"}, 5)
    add("PhaseChanged", {"phase": "awaiting-ci", "reason": "qa passed"}, 5)
    if log_records is not None:
        d = store.agent_logs_dir(home, KEY) if hasattr(store, "agent_logs_dir") else home / "agent-logs" / KEY
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"reconcile-{KEY}-{T0 + 61:.6f}.stream.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in log_records) + "\n")
        os.utime(p, (T0 + 120, T0 + 120))


def _rows(home):
    tr = rounds_view.build(home, KEY, now=T0 + 1000)
    return tr, next(r for r in tr.rows if r.label == "QA")


def _tool(name, **inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "i", "name": name, "input": inp}]}}


def test_ran_with_findings_and_skill_call(home, monkeypatch):
    note = {"text": "blocking: a\nminor b\nminor c", "review_skill": "demo", "status": "ran"}
    _seed(home, monkeypatch, note, [_tool("Skill", skill="demo")])
    tr, row = _rows(home)
    assert "review: ran `demo` · 3 findings (1 blocking)" in row.outcome
    assert "no Skill call" not in row.outcome
    assert tr.review.startswith("ran `demo` · 3 findings")


def test_ran_without_skill_call_flags_mismatch(home, monkeypatch):
    note = {"text": "", "review_skill": "demo", "status": "ran"}
    _seed(home, monkeypatch, note, [_tool("Read", file_path="/x")])
    _, row = _rows(home)
    assert "0 findings" in row.outcome
    assert "reported ran; no Skill call in log" in row.outcome


def test_skipped_and_error(home, monkeypatch):
    _seed(home, monkeypatch, {"text": "runner cannot run skills", "review_skill": "demo",
                                  "status": "skipped"}, None)
    tr, row = _rows(home)
    assert "review: skipped: runner cannot run skills" in row.outcome
    assert tr.review == "skipped: runner cannot run skills"


def test_error(home, monkeypatch):
    _seed(home, monkeypatch, {"text": "skill crashed", "review_skill": "demo", "status": "error"}, None)
    _, row = _rows(home)
    assert "review: error: skill crashed" in row.outcome
