"""GA-2: `maestro append --type <T>` accepted any T and wrote it straight to the log,
so every invariant `ops` enforces (max_impl_turns ceiling, unverified-AC gate,
independent QA gate, backoff/dead-letter routing, RB-12's captured-test-run gate,
T-79's per-AC captured-check gate) was one raw append away from irrelevant.
`cmd_append` now refuses the ops-owned types -- most with an error naming the verb
to use instead, and T-79's `AcCheckCaptured` (dispatcher-owned, no manual verb at
all) with its own message -- a denylist, not an allowlist, so the legitimate raw
appends (`events.SIDE_EFFECTING`, plus ad-hoc types like Note and
ResearchProposed, and AD-7's now-historical-only `Approved`) keep working
unchanged.
"""
import pytest

from maestro import cli, event_log
from maestro import events as E
from maestro import ops
from maestro import snapshot as snap_mod

from conftest import seed_ticket

DENIED = [
    (E.IMPL_TURN, "impl-turn"),
    (E.PHASE_CHANGED, "set-phase"),
    (E.AC_VERIFIED, "verify-ac"),
    (E.AC_QA_VERDICT, "qa-verdict"),
    (E.FAILED, "fail"),
    (E.STALLED, "fail"),
    (E.TEST_RUN_CAPTURED, "capture-tests"),
]


# ---------------------------------------------------------------------------
# The ops-owned types with a real verb refuse, name it, and append nothing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("denied_type, verb", DENIED)
def test_denied_type_refuses_and_appends_nothing(home, cfg, capsys, denied_type, verb):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    before_events = event_log.read(home, "T-1")
    before_snap = snap_mod.load(home, "T-1").to_dict()

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", denied_type,
                   "--payload", "{}"])

    assert rc == 2
    err = capsys.readouterr().err
    assert verb in err

    after_events = event_log.read(home, "T-1")
    after_snap = snap_mod.load(home, "T-1").to_dict()
    assert after_events == before_events
    assert after_snap == before_snap


def test_ac_check_captured_refuses_with_dispatcher_owned_message(home, cfg, capsys):
    """T-79: AcCheckCaptured has no manual verb at all -- only
    `ops.run_ac_checks` (the dispatcher's own verifying stage) ever appends
    it -- so its refusal message is its own, not `use maestro <verb>`."""
    seed_ticket(home, "T-1", "do the thing", phase="implementing")
    before_events = event_log.read(home, "T-1")

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", "AcCheckCaptured",
                   "--payload", "{}"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "dispatcher-owned" in err
    assert event_log.read(home, "T-1") == before_events


def test_denylist_covers_exactly_the_documented_types():
    from maestro.cli import _APPEND_DENYLIST
    assert set(_APPEND_DENYLIST.keys()) == {t for t, _ in DENIED} | {E.AC_CHECK_CAPTURED}
    for t in E.SIDE_EFFECTING:
        assert t not in _APPEND_DENYLIST


# ---------------------------------------------------------------------------
# The legitimate raw-append paths keep working: SIDE_EFFECTING types, plus the
# ad-hoc types the skills use (Note, ResearchProposed).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("allowed_type, payload, step_id", [
    # step-id matches the real idiom implementing.md:181 uses: `pr-$KEY`.
    ("PrOpened", '{"number": 7, "url": "https://github.com/cortop/maestro/pull/7", "draft": true}', "pr-T-1"),
    ("Note", '{"text": "breadcrumb"}', "note-T-1"),
    ("ResearchProposed", '{"text": "proposal"}', "research-T-1"),
    # AD-7: Approved is historical-only now (no `ops.approve` owns it anymore),
    # so a raw append of it is no longer denied -- it just folds inertly.
    ("Approved", "{}", "approve-T-1"),
])
def test_allowed_type_still_appends_and_folds(home, cfg, allowed_type, payload, step_id):
    seed_ticket(home, "T-1", "do the thing", phase="implementing")

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", allowed_type,
                   "--payload", payload, "--step-id", step_id])

    assert rc == 0
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == allowed_type]
    assert len(evs) == 1
    snap_mod.load(home, "T-1")  # folds without raising


# ---------------------------------------------------------------------------
# End-to-end regression: a raw PhaseChanged can no longer smuggle a ticket past
# the unverified-AC gate or a failing QA verdict into awaiting-ci.
# ---------------------------------------------------------------------------

def test_raw_phase_changed_cannot_bypass_qa_failing_gate(home, cfg):
    store_spec = home / "tickets" / "T-1" / "spec.md"
    seed_ticket(home, "T-1", "do the thing", phase="qa")
    store_spec.parent.mkdir(parents=True, exist_ok=True)
    store_spec.write_text(
        "# T-1\napproval_tier: 1\n\n## Intent\ndo the thing\n\n"
        "## Acceptance criteria\n- [ ] it works\n")
    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "diff does not satisfy AC")

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", "PhaseChanged",
                   "--payload", '{"phase": "awaiting-ci"}'])

    assert rc == 2
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == "qa"
