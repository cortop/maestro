"""T-80: detect AC-less specs deterministically -- the dispatcher due-gate
parks a zero-AC ticket before any spawn, `ops.set_phase`/`ops.qa_brief` fail
closed on zero ACs, `maestro doctor`'s `check_missing_acs` is the report-only
safety net, and `maestro create` warns (never blocks) at mint time.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

from maestro import cli as cli_mod, dispatcher as disp, event_log, health, inbox, ops, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

# The two zero-AC shapes T-80's spec names: T-79's bare bullet (no checkbox --
# `_AC_RE` doesn't match it at all) and the seed template's own dangling
# checkbox (matches once, with empty captured text).
BARE_DASH_AC = "- "
BLANK_CHECKBOX_AC = "- [ ] "
REAL_AC = "- [ ] it works"


def _seed(home, key, *, phase=Phase.READY, ac_body=REAL_AC):
    spec = (f"# {key}\napproval_tier: 0\ndependsOn: []\n\n"
            f"## Acceptance criteria\n{ac_body}\n")
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _rewrite_spec(home, key, ac_body):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 0\ndependsOn: []\n\n"
                       f"## Acceptance criteria\n{ac_body}\n")


# --- AC1/AC2: dispatcher due-gate -- park before any spawn ------------------

@pytest.mark.parametrize("ac_body", [BARE_DASH_AC, BLANK_CHECKBOX_AC],
                         ids=["bare-dash-bullet", "blank-checkbox"])
def test_dispatch_parks_ac_less_spec_before_any_spawn(home, cfg, ac_body):
    """AC1/AC2: a real dispatch() sweep over a temp home with an AC-less spec
    -- T-79's bare '- ' bullet, or the seed template's own dangling '- [ ] '
    -- parks the ticket: the canned missing-acs-<key> question is appended
    and the phase moves to awaiting-human, with NO session spawned, asserted
    on the spawn ledger itself."""
    _seed(home, "T-1", phase=Phase.READY, ac_body=ac_body)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "T-1" not in report.spawned
    assert report.due == []

    events = event_log.read(home, "T-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert asked[0]["payload"]["qid"] == "missing-acs-T-1"
    assert not any(e["type"] == "Failed" for e in events)

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value

    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    assert "T-1" not in ledger


def test_dispatch_missing_acs_park_appends_nothing_new_on_a_later_sweep(home, cfg):
    """Once parked, the ticket is sleeping (awaiting-human, one open question,
    no answer yet) -- `is_due` itself keeps it out of the per-key loop's
    missing-acs check on every later sweep, so no duplicate QuestionAsked or
    PhaseChanged ever gets appended just from the ticket sitting there."""
    _seed(home, "T-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    events_after_first = event_log.read(home, "T-1")

    disp.dispatch(cfg, DryRunSessions(), now=1001)
    disp.dispatch(cfg, DryRunSessions(), now=1002)
    events_after_more = event_log.read(home, "T-1")

    assert len(events_after_more) == len(events_after_first)


def test_dispatch_missing_acs_park_applies_to_triaging_too(home, cfg):
    """The gate isn't scoped to just `ready` -- the spec's Intent names mint,
    triaging, pickup-approval, AND worktree setup as the phases a zero-AC
    ticket currently sails through uncaught; `triaging` (freshly minted,
    before any approval round) must be caught just as early."""
    _seed(home, "T-1", phase=Phase.TRIAGING, ac_body=BARE_DASH_AC)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "T-1" not in report.spawned
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value


def test_dispatch_terminal_zero_ac_ticket_is_never_parked(home, cfg):
    """Backward-compat (spec Notes): a DONE/terminal ticket is never flagged,
    however few ACs its spec parses to -- no spec rewrites, no surprise park
    on finished work."""
    _seed(home, "T-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    event_log.append(home, "T-1", "Finalized", {}, actor="r")
    snap_mod.rebuild(home, "T-1")
    assert snap_mod.load(home, "T-1").phase == Phase.DONE.value

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "T-1" not in report.spawned
    asked = [e for e in event_log.read(home, "T-1") if e["type"] == "QuestionAsked"]
    assert asked == []


# --- AC3: human fills in ACs + answers -> the next sweep proceeds normally --

def test_dispatch_proceeds_normally_after_human_fills_acs_and_answers(home, cfg):
    _seed(home, "T-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_HUMAN.value

    _rewrite_spec(home, "T-1", REAL_AC)
    rc = cli_mod.main(["--home", str(home), "ans", "T-1", "done",
                       "--qid", "missing-acs-T-1", "--no-nudge"])
    assert rc == 0

    report = disp.dispatch(cfg, DryRunSessions(), now=1001)

    assert "T-1" in report.spawned


def test_dispatch_inbox_answer_bypasses_the_park_even_before_the_spec_is_fixed(home, cfg):
    """A pending human answer must fall through to a real spawn regardless of
    what the missing-acs re-check would say -- that spawn is what actually
    folds the inbox and re-routes the ticket; re-parking on the very sweep
    meant to process the human's answer would wedge it forever."""
    _seed(home, "T-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_HUMAN.value

    # Human answers WITHOUT fixing the spec (still zero ACs).
    rc = cli_mod.main(["--home", str(home), "ans", "T-1", "oops, forgot the ACs",
                       "--qid", "missing-acs-T-1", "--no-nudge"])
    assert rc == 0

    report = disp.dispatch(cfg, DryRunSessions(), now=1001)

    assert "T-1" in report.spawned


# --- AC4: ops.set_phase refuses qa/awaiting-ci with zero ACs ---------------

def test_set_phase_refuses_qa_handoff_on_zero_acs_no_event_appended(home, cfg):
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, ac_body=BARE_DASH_AC)
    before = len(event_log.read(home, "T-1"))

    rc = cli_mod.main(["--home", str(home), "set-phase", "T-1", "qa"])

    assert rc != 0
    assert len(event_log.read(home, "T-1")) == before
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value


def test_set_phase_refuses_awaiting_ci_on_zero_acs_no_event_appended(home, cfg):
    _seed(home, "T-1", phase=Phase.QA, ac_body=BLANK_CHECKBOX_AC)
    before = len(event_log.read(home, "T-1"))

    rc = cli_mod.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci"])

    assert rc != 0
    assert len(event_log.read(home, "T-1")) == before
    assert snap_mod.load(home, "T-1").phase == Phase.QA.value


def test_set_phase_qa_handoff_unaffected_with_a_real_ac(home, cfg):
    """With >=1 real AC, behavior is byte-identical to today -- the same
    ticket that refuses above (only its spec's AC text differs) now succeeds
    normally."""
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, ac_body=REAL_AC)

    rc = cli_mod.main(["--home", str(home), "set-phase", "T-1", "qa"])

    assert rc == 0
    assert snap_mod.load(home, "T-1").phase == Phase.QA.value


def test_set_phase_missing_acs_refusal_is_not_overridable_by_force(home, cfg):
    """Unconditional, same posture as the existing QA-failing gate -- `force`
    is for overriding the unattested-ACs count, not for making up ACs that
    don't exist."""
    _seed(home, "T-1", phase=Phase.QA, ac_body=BARE_DASH_AC)

    with pytest.raises(store.MaestroError):
        ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, force=True)

    assert snap_mod.load(home, "T-1").phase == Phase.QA.value


# --- AC5: ops.qa_brief errors instead of minting an empty packet -----------

def test_qa_brief_cli_exits_nonzero_with_a_clear_message_on_zero_acs(home, cfg, capsys):
    _seed(home, "T-1", phase=Phase.QA, ac_body=BARE_DASH_AC)

    rc = cli_mod.main(["--home", str(home), "qa-brief", "T-1"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "no acceptance criteria" in err


def test_qa_brief_also_refuses_the_blank_checkbox_shape(home, cfg):
    _seed(home, "T-1", phase=Phase.QA, ac_body=BLANK_CHECKBOX_AC)

    with pytest.raises(store.MaestroError):
        ops.qa_brief(cfg, "T-1")


# --- AC6: maestro doctor's check_missing_acs --------------------------------

def test_check_missing_acs_warns_non_terminal_zero_ac_tickets_skips_terminal(home, cfg):
    _seed(home, "BAD-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    _seed(home, "BAD-2", phase=Phase.IMPLEMENTING, ac_body=BLANK_CHECKBOX_AC)
    _seed(home, "OK-1", phase=Phase.READY, ac_body=REAL_AC)
    _seed(home, "DONE-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    event_log.append(home, "DONE-1", "Finalized", {}, actor="r")
    snap_mod.rebuild(home, "DONE-1")

    result = health.check_missing_acs(cfg, 1000)

    assert result["status"] == "warn"
    assert sorted(result["keys"]) == ["BAD-1", "BAD-2"]
    assert "OK-1" not in result["keys"]
    assert "DONE-1" not in result["keys"]


def test_check_missing_acs_ok_when_every_spec_carries_acs(home, cfg):
    _seed(home, "OK-1", phase=Phase.READY, ac_body=REAL_AC)
    result = health.check_missing_acs(cfg, 1000)
    assert result["status"] == "ok"
    assert result["keys"] == []


def test_check_missing_acs_registered_in_the_doctor_check_registry(home, cfg):
    """Real CLI, matching the project's own QA convention."""
    _seed(home, "BAD-1", phase=Phase.READY, ac_body=BARE_DASH_AC)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        cli_mod.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    rpt = json.loads(buf.getvalue())
    names = [c["name"] for c in rpt["checks"]]
    assert "missing_acs" in names
    check = next(c for c in rpt["checks"] if c["name"] == "missing_acs")
    assert check["status"] == "warn"
    assert "BAD-1" in check["keys"]


# --- AC7: maestro create warns (never blocks) on a zero-AC spec ------------

def test_json_create_warns_on_the_freshly_seeded_zero_ac_spec(home, cfg, capsys):
    """The dispatcher's own `_seed_spec` template always seeds a dangling
    '- ' with no checkbox at all -- there's no `--ac` flag, so a plain
    `--json` create always reproduces this shape today; warn, never block."""
    rc = cli_mod.main(["--home", str(home), "create", "A fresh ticket",
                       "--json", "--no-nudge"])
    assert rc == 0  # never blocks
    err = capsys.readouterr().err
    assert "no acceptance criteria" in err


def test_warn_missing_acs_is_silent_when_the_spec_carries_a_real_ac(capsys):
    cli_mod._warn_missing_acs(f"# T-1\n\n## Acceptance criteria\n{REAL_AC}\n")
    assert capsys.readouterr().err == ""


def test_warn_missing_acs_fires_for_both_zero_ac_shapes(capsys):
    cli_mod._warn_missing_acs(f"# T-1\n\n## Acceptance criteria\n{BARE_DASH_AC}\n")
    assert "no acceptance criteria" in capsys.readouterr().err

    cli_mod._warn_missing_acs(f"# T-1\n\n## Acceptance criteria\n{BLANK_CHECKBOX_AC}\n")
    assert "no acceptance criteria" in capsys.readouterr().err


def test_editor_seeded_flow_warns_when_the_ac_section_is_left_untouched(monkeypatch, capsys):
    """`_editor_intent` seeds `_SEED_SPEC_TEMPLATE` (its own dangling
    '- [ ] ') into the temp file; `EDITOR=true` simulates a human who saved
    without touching it."""
    monkeypatch.setenv("EDITOR", "true")
    cli_mod._editor_intent("My ticket", 3)
    assert "no acceptance criteria" in capsys.readouterr().err


# --- AC8: a sweep over a home whose specs all carry ACs is unaffected ------

def test_dispatch_never_intercepts_a_ticket_whose_spec_has_real_acs(home, cfg):
    _seed(home, "T-1", phase=Phase.READY, ac_body=REAL_AC)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == ["T-1"]
    asked = [e for e in event_log.read(home, "T-1") if e["type"] == "QuestionAsked"]
    assert asked == []
    assert snap_mod.load(home, "T-1").phase == Phase.READY.value  # unchanged by this gate
