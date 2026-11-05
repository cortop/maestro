"""The implementing skill names every round the dispatcher routes into it, and
the reasons those routes record agree with the skill's sync rule."""
from __future__ import annotations

from pathlib import Path

from conftest import seed_phase
from maestro import event_log, ops, outcomes
from maestro import snapshot as snap_mod
from maestro.statemachine import Phase

SKILL = (Path(__file__).resolve().parents[1] / ".claude" / "commands"
         / "maestro-reconcile-implementing.md")


def _text() -> str:
    return " ".join(SKILL.read_text(encoding="utf-8").split())


def _case(start: str) -> str:
    """One `- **...` hand-off bullet of step 5."""
    return _text().split(start, 1)[1].split("- **", 1)[0]


def test_stale_reason_says_sync_not_rebase(cfg):
    seed_phase(cfg.home, "T-1", Phase.AWAITING_CI)
    assert ops.route_stale(cfg, "T-1", base_branch="main", policy="always", actor="dispatcher")
    snap = snap_mod.load(cfg.home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    reason = [e for e in event_log.read(cfg.home, "T-1")
              if e["type"] == "PhaseChanged"][-1]["payload"]["reason"]
    assert "origin/main advanced (policy=always)" in reason
    assert "rebase" not in reason
    assert outcomes._round_cause(reason, "awaiting-ci") == "conflict"


def test_step_zero_keeps_merge_for_pushed_branches():
    text = _text()
    assert "merge base in; never rebase" in text
    assert "`origin/<BASE> advanced (policy=...)`" in text


def test_ci_round_is_a_named_case_in_step_one():
    text = _text()
    assert "**If CI sent you back**" in text
    assert "`PR #<n>: CI failing:` on a stack" in text
    assert "**CI-fix round**" in text


def test_ready_pr_ci_round_goes_to_awaiting_ci():
    seg = _case("`CI failing:` round on a ready PR")
    assert "`pr_draft` false" in seg
    assert 'set-phase "$KEY" awaiting-ci' in " ".join(seg.split())


def test_draft_ci_round_goes_to_qa_and_counts_a_turn():
    seg = _case("`CI failing:` round on a still-draft PR")
    assert 'set-phase "$KEY" qa' in seg
    assert "`qa` fix round or a draft PR's `CI failing:` round, record it now" in _text()
