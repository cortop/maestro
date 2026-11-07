"""A degraded -> ready detour resumes the implementing round it interrupted, but only with a PR."""
from __future__ import annotations

import json

from conftest import seed_ticket
from maestro import cli, event_log, implementing_brief, ops, outcomes, snapshot as snap_mod

KEY = "T-1"


def _phase(home, phase, reason="x"):
    event_log.append(home, KEY, "PhaseChanged", {"phase": phase, "reason": reason}, actor="r")


def _detour(home, *, pr, draft=True):
    seed_ticket(home, KEY, "resume", phase="ready")
    if pr:
        event_log.append(home, KEY, "PrOpened", {"number": 7, "url": "https://x/7", "draft": True}, actor="r")
        if not draft:
            event_log.append(home, KEY, "PrUpdated", {"number": 7, "draft": False}, actor="d")
    _phase(home, "implementing", "changes requested: rename it")
    _phase(home, "degraded", "boom")
    _phase(home, "ready", "human retry")
    _phase(home, "implementing", "worktree ready")
    snap_mod.rebuild(home, KEY)


def test_pr_detour_resumes_review_feedback(home):
    _detour(home, pr=True)
    assert implementing_brief.current_round(home, KEY) == ("review-feedback", "changes requested: rename it")


def test_no_pr_stays_fresh(home):
    _detour(home, pr=False)
    assert implementing_brief.current_round(home, KEY)[0] == "fresh"


def test_handoff_goes_to_awaiting_ci(home, capsys, monkeypatch):
    _detour(home, pr=True, draft=False)
    seen = []
    monkeypatch.setattr(ops, "set_phase", lambda cfg, key, phase, **kw: seen.append(phase.value) or {"ok": 1})
    assert cli.main(["--home", str(home), "handoff", KEY]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["round"] == "review-feedback" and out["to"] == "awaiting-ci"
    assert seen == ["awaiting-ci"]


def test_brief_prints_review_feedback(home, capsys):
    _detour(home, pr=True)
    assert cli.main(["--home", str(home), "brief", KEY]) == 0
    out = capsys.readouterr().out
    assert '"round": "review-feedback"' in out
    assert "## Round: review-feedback " in out and "## Round: fresh " not in out


def test_scorecard_counts_one_round(home):
    _detour(home, pr=True)
    t = outcomes.fold_ticket(None, home, KEY, None)
    assert [r["cause"] for r in t["rounds"]] == ["review"]
