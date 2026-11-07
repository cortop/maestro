"""event_summary leads with the fields a human needs, per event type."""
from __future__ import annotations

from maestro.tui.events import render_event
from maestro.view.events import event_summary


def _s(type_, payload):
    return event_summary({"type": type_, "payload": payload})


def test_ac_qa_verdict_reads_index_verdict_and_first_evidence_line():
    p = {"ac_hash": "h", "ac_index": 2, "ac_text": "t", "verdict": "fail",
         "evidence": "no test covers it\nsecond line", "axis": "x"}
    assert _s("AcQaVerdict", p) == "AC 2 FAIL: no test covers it"
    assert _s("AcQaVerdict", {**p, "verdict": "pass"}).startswith("AC 2 PASS: ")


def test_ac_verified_what_where_result():
    p = {"ac_index": 1, "evidence": {"what": "ran pytest", "where": "tests/t.py", "result": "ok"}}
    assert _s("AcVerified", p) == "AC 1: ran pytest @ tests/t.py → ok"


def test_review_events_lead_with_body():
    assert _s("ReviewReplyPosted", {"comment_id": 5, "kind": "reply", "body": "fixed\nmore"}) == "fixed"
    fb = {"author": "alice", "path": "a.py", "line": 7, "body": "rename this", "state": "x"}
    assert _s("ReviewFeedbackReceived", fb) == "alice a.py:7 rename this"


def test_note_uses_first_non_empty_text_key():
    assert _s("Note", {"text": "", "note": "", "summary": "sum", "body": "b"}) == "sum"
    assert _s("Note", {"body": "only body"}) == "only body"


def test_post_qa_skill_spawned_shows_skill():
    assert _s("PostQaSkillSpawned", {"skill": "synthetic-skill", "tree_key": "k"}) == "synthetic-skill"


def test_cap_first_line_nondict_and_fallback():
    assert len(_s("Note", {"text": "x" * 500})) <= 160
    assert event_summary({"type": "Note", "payload": "oops"}) == ""
    assert event_summary({"type": "Note", "payload": ["a"]}) == ""
    assert _s("Unknown", {"a": 1, "b": 2, "c": 3, "d": 4}) == "a=1, b=2, c=3"


def test_render_event_uses_summary_and_escapes_markup():
    ev = {"seq": 1, "ts": "2026-01-01T00:00:00", "type": "Note", "actor": "a",
          "payload": {"text": "see [bold]x[/bold]"}}
    line = render_event(ev)
    assert "\\[bold]x\\[/bold]" in line
