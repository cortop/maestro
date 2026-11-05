"""`maestro handoff` picks qa or awaiting-ci from the implementing round and pr_draft, and the
implementing skill drives the plumbing through verbs instead of hand-typed events."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maestro import cli, event_log, ops, snapshot as snap_mod

from conftest import seed_ticket

KEY = "T-1"
ROOT = Path(__file__).resolve().parent.parent


def _seed(home, prev, reason, draft):
    seed_ticket(home, KEY, "handoff", phase=prev, pr=7)
    if draft is False:
        event_log.append(home, KEY, "PrUpdated", {"number": 7, "draft": False}, actor="d")
    event_log.append(home, KEY, "PhaseChanged", {"phase": "implementing", "reason": reason}, actor="r")
    snap_mod.rebuild(home, KEY)


CASES = [
    ("ready", "worktree ready", True, "fresh", "qa"),
    ("qa", "qa: AC 1 fails", True, "qa-fix", "qa"),
    ("awaiting-ci", "CI failing: lint", True, "ci-fix", "qa"),
    ("awaiting-ci", "CI failing: lint", False, "ci-fix", "awaiting-ci"),
    ("in-review", "changes requested: rename it", True, "review-feedback", "awaiting-ci"),
    ("awaiting-ci", "PR #7 is CONFLICTING", True, "conflict", "awaiting-ci"),
    ("awaiting-ci", "origin/main advanced (policy=sync)", False, "base-drift", "awaiting-ci"),
    ("awaiting-human", "pr split decision: approve", True, "split-decision", "qa"),
]


@pytest.mark.parametrize("prev,reason,draft,rnd,expected", CASES)
def test_handoff_routes_each_round(home, monkeypatch, capsys, prev, reason, draft, rnd, expected):
    _seed(home, prev, reason, draft)
    seen = []
    monkeypatch.setattr(ops, "set_phase", lambda cfg, key, phase, **kw: seen.append(phase.value) or {"ok": 1})
    assert cli.main(["--home", str(home), "handoff", KEY]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["round"] == rnd and out["to"] == expected and seen == [expected]


def test_handoff_refuses_unknown_round_and_missing_pr(home, capsys):
    _seed(home, "awaiting-human", "human said go", True)
    assert cli.main(["--home", str(home), "handoff", KEY]) != 0
    assert "which implementing round" in capsys.readouterr().err
    assert snap_mod.load(home, KEY).phase == "implementing"
    seed_ticket(home, "T-2", "nopr", phase="ready")
    event_log.append(home, "T-2", "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, actor="r")
    snap_mod.rebuild(home, "T-2")
    assert cli.main(["--home", str(home), "handoff", "T-2"]) != 0
    assert "open-pr" in capsys.readouterr().err


def test_handoff_really_moves_to_qa(home, capsys):
    _seed(home, "ready", "worktree ready", True)
    assert cli.main(["--home", str(home), "handoff", KEY]) == 0
    assert json.loads(capsys.readouterr().out)["to"] == "qa"
    assert snap_mod.load(home, KEY).phase == "qa"


def test_skill_uses_verbs():
    text = "\n".join(p.read_text() for p in [ROOT / ".claude/commands/maestro-reconcile-implementing.md",
                                             *(ROOT / "maestro/_implementing_rounds").glob("*.md")])
    assert "PrOpened" not in text and "--type PrOpened" not in text
    for verb in ("maestro sync-base", "maestro open-pr", "maestro handoff"):
        assert verb in text
    assert not re.search(r"git -C <WT> (merge|rebase)\b", text)
    assert "gh pr create" not in text
