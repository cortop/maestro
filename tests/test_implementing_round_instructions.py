"""The implementing skill names every round the dispatcher routes into it, and
the reasons those routes record agree with the skill's sync rule."""
from __future__ import annotations

from conftest import seed_phase
from maestro import event_log, implementing_brief, ops, outcomes
from maestro import snapshot as snap_mod
from maestro.statemachine import Phase


def _round(name: str) -> str:
    return " ".join(implementing_brief.instructions(name).split())


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


def test_sync_keeps_merge_for_pushed_branches():
    text = _round("sync")
    assert "merge base in; never rebase" in text
    assert "`origin/<BASE> advanced (policy=...)`" in text


def test_ci_round_is_a_named_round():
    text = _round("ci-fix")
    assert "`PR #<n>: CI failing:` (on a stack)" in text or "`PR #<n>: CI failing:` on a stack" in text
    assert "**CI-fix round**" in text


def test_ready_pr_ci_round_goes_to_awaiting_ci():
    seg = _round("ci-fix").split("**Ready PR**", 1)[1].split("- **", 1)[0]
    assert "`pr_draft` false" in seg
    assert 'set-phase "$KEY" awaiting-ci' in seg


def test_draft_ci_round_goes_to_qa_and_counts_a_turn():
    text = _round("ci-fix")
    seg = text.split("**Still-draft PR**", 1)[1].split("- **", 1)[0]
    assert 'set-phase "$KEY" qa' in seg
    assert "maestro impl-turn" in text and "On a still-draft PR" in text
