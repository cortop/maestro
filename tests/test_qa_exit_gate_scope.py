"""T-97: the QA/AC completeness gate (T-85) keyed off the DESTINATION being
`awaiting-ci` only, so `set-phase in-review` and `finalize` -- both verbs already
granted to the `implementing` phase, both legal `TRANSITIONS[Phase.IMPLEMENTING]`
edges -- skipped it entirely (bypass 1/2). Symmetrically, because the old gate
fired on ANY source phase, it also blocked the `awaiting-human` skill's only
prescribed stranded-recovery action (`set-phase awaiting-ci`), with no way to
satisfy it from that phase (bypass 3). This ticket rescopes the gate to
(source, destination) jointly: active for a step LEAVING `implementing`/`qa`
TOWARD `awaiting-ci`/`in-review`/`done`, exempt everywhere else.
"""
from __future__ import annotations

import pytest

from maestro import cli, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.statemachine import TRANSITIONS, Phase

SPEC_TEMPLATE = """\
# {key}: {title}

priority: 2

## Intent
{intent}

## Acceptance criteria
{acs}
"""


def _write_spec(home, key, *, title="Test ticket", intent="do the thing",
                acs=("build the widget",)):
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       SPEC_TEMPLATE.format(key=key, title=title, intent=intent, acs=ac_lines))


def _create(cfg, key, **spec_kwargs):
    _write_spec(cfg.home, key, **spec_kwargs)
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": spec_kwargs.get("title", "Test ticket"), "source": "test"}, actor="d")
    return snap_mod.rebuild(cfg.home, key)


def _evidence(what="ran pytest", where="tests/test_widget.py::test_builds", result="PASSED"):
    return {"what": what, "where": where, "result": result}


def _implementing_with_verified_acs(cfg, key, n=1):
    _create(cfg, key, acs=tuple(f"do thing {i}" for i in range(1, n + 1)))
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")
    for i in range(1, n + 1):
        ops.verify_ac(cfg, key, i, _evidence())


# ---------------------------------------------------------------------------
# AC1: `set-phase in-review` from `implementing` is gated exactly like
# `awaiting-ci` -- same refusal message shape, no event appended.
# ---------------------------------------------------------------------------

def test_in_review_refuses_when_acs_lack_passing_qa_verdict(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1", n=2)
    before = event_log.read(home, "T-1")

    # T-97 AC1: the message is byte-identical to the awaiting-ci refusal's own
    # shape -- it names "awaiting-ci" regardless of the actual destination,
    # deliberately (the two gates are literally the same function call).
    with pytest.raises(store.MaestroError,
                       match="refusing awaiting-ci — 2 acceptance criteria have no passing "
                             "spec-axis QA verdict"):
        ops.set_phase(cfg, "T-1", Phase.IN_REVIEW, reason="tests green")

    after = event_log.read(home, "T-1")
    assert after == before  # byte-identical -- no PhaseChanged, no Note
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value


def test_in_review_refuses_via_real_cli_no_event_appended(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1")
    seq_before = snap_mod.load(home, "T-1").observed_seq

    rc = cli.main(["--home", str(home), "set-phase", "T-1", "in-review", "--reason", "tests green"])

    assert rc != 0
    assert not [e for e in event_log.read(home, "T-1", since=seq_before) if e["type"] == "PhaseChanged"]
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value


def test_in_review_succeeds_once_qa_passes(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff builds the widget; tests green")

    ops.set_phase(cfg, "T-1", Phase.IN_REVIEW, reason="qa: all ACs pass")

    assert snap_mod.load(home, "T-1").phase == Phase.IN_REVIEW.value


# ---------------------------------------------------------------------------
# AC2: `finalize` from `implementing` on a non-`mode: local` binding is gated
# the same way -- unverified ACs OR missing QA verdicts refuse it, no
# `Finalized` event. `mode: local`'s finalize is covered unchanged by
# tests/test_local_mode.py::test_local_mode_ticket_reaches_done_with_backup_on_write
# (reaches `done` with zero qa-verdicts, exactly as before this ticket).
# ---------------------------------------------------------------------------

def test_finalize_refuses_from_implementing_with_unverified_acs(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    before = event_log.read(home, "T-1")

    with pytest.raises(store.MaestroError, match="refusing done — 1 acceptance criteria unverified"):
        ops.finalize(cfg, "T-1")

    after = event_log.read(home, "T-1")
    assert after == before
    assert not [e for e in after if e["type"] == "Finalized"]
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value


def test_finalize_refuses_from_implementing_with_missing_qa_verdict(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1")
    before = event_log.read(home, "T-1")

    with pytest.raises(store.MaestroError,
                       match="acceptance criteria have no passing spec-axis QA verdict"):
        ops.finalize(cfg, "T-1")

    after = event_log.read(home, "T-1")
    assert after == before
    assert not [e for e in after if e["type"] == "Finalized"]


def test_finalize_refuses_via_real_cli_no_event_appended(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    seq_before = snap_mod.load(home, "T-1").observed_seq

    rc = cli.main(["--home", str(home), "finalize", "T-1"])

    assert rc != 0
    assert not [e for e in event_log.read(home, "T-1", since=seq_before) if e["type"] == "Finalized"]
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value


def test_finalize_succeeds_from_implementing_once_qa_passes(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff builds the widget; tests green")

    ops.finalize(cfg, "T-1")

    assert snap_mod.load(home, "T-1").phase == Phase.DONE.value


# ---------------------------------------------------------------------------
# AC3: a stranded `awaiting-human` ticket (pr_number set, both ACs
# verify-ac'd, zero qa-verdicts) can complete the recovery its own skill
# prescribes (`set-phase awaiting-ci --requeue 60`) -- the gate no longer
# fires for this source phase, and the ticket is no longer due=stranded.
# ---------------------------------------------------------------------------

def test_stranded_awaiting_human_recovery_reaches_awaiting_ci(cfg):
    home = cfg.home
    _implementing_with_verified_acs(cfg, "T-1", n=2)
    event_log.append(home, "T-1", "PrOpened", {"number": 7, "url": "https://example/pull/7", "draft": True},
                     actor="reconciler", step_id="pr-T-1")
    ops.set_phase(cfg, "T-1", Phase.AWAITING_HUMAN, reason="stranded test setup")

    snap = snap_mod.load(home, "T-1")
    due = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert due.due and due.reason == "stranded"  # confirms the repro shape first

    # The skill's own prescribed recovery -- skills/maestro-reconcile-awaiting-human.md.
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="stranded recovery", requeue_in=60)

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value
    due = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=snap.spec_hash, now=1000)
    assert due.reason != "stranded"


# ---------------------------------------------------------------------------
# AC4: `awaiting_ci_qa_gate = false` reverts both NEW refusals byte-identically
# to HEAD -- neither ever gated `in-review`/`finalize` before this ticket.
# ---------------------------------------------------------------------------

def test_in_review_gate_off_reverts_to_head(cfg):
    cfg.awaiting_ci_qa_gate = False
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    # zero verify-ac, zero qa-verdicts -- HEAD let this straight through.

    ops.set_phase(cfg, "T-1", Phase.IN_REVIEW, reason="tests green")

    assert snap_mod.load(home, "T-1").phase == Phase.IN_REVIEW.value


def test_finalize_gate_off_reverts_to_head(cfg):
    cfg.awaiting_ci_qa_gate = False
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.finalize(cfg, "T-1")

    assert snap_mod.load(home, "T-1").phase == Phase.DONE.value


# ---------------------------------------------------------------------------
# AC5: the whole `implementing` transition surface is enumerated -- a newly
# granted, ungated escape to a completion/review phase fails this test rather
# than shipping silently.
# ---------------------------------------------------------------------------

def test_implementing_transition_surface_is_fully_gated_or_known_safe(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    # Detours that leave `implementing` without claiming the work is done or
    # ready for human review -- untouched by the QA/AC completeness gate by
    # design (QA is where the independent review itself happens; VERIFYING is
    # the RB-14 test-run detour; DEGRADED/AWAITING_HUMAN/TERMINATING are all
    # non-completion escapes with their own handling).
    ungated_detours = {Phase.QA, Phase.VERIFYING, Phase.DEGRADED, Phase.AWAITING_HUMAN,
                       Phase.TERMINATING}
    gated_completion_or_review = {Phase.AWAITING_CI, Phase.IN_REVIEW, Phase.DONE}

    destinations = TRANSITIONS[Phase.IMPLEMENTING]
    unclassified = destinations - ungated_detours - gated_completion_or_review
    assert not unclassified, (
        f"{unclassified} is a new IMPLEMENTING transition that is neither a known "
        "ungated detour nor one of the three gated completion phases -- classify "
        "it explicitly (T-97)")

    for dest in sorted(gated_completion_or_review, key=lambda p: p.value):
        with pytest.raises(store.MaestroError, match="acceptance criteria unverified"):
            ops.set_phase(cfg, "T-1", dest, reason="probe")
        assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value
