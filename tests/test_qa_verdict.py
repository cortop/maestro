"""AD-4: independent QA verdicts (AcQaVerdict events) and the enforced gate
that a failing verdict blocks `implementing -> awaiting-ci`.
"""
import pytest

from maestro import cli, event_log, ops, snapshot as snap_mod, store
from maestro.statemachine import Phase

SPEC_TEMPLATE = """\
# {key}: {title}

approval_tier: 1
priority: 2

## Intent
{intent}

## Acceptance criteria
{acs}
"""


def _write_spec(home, key, *, title="Test ticket", intent="do the thing",
                acs=("build the widget", "document it")):
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       SPEC_TEMPLATE.format(key=key, title=title, intent=intent, acs=ac_lines))


def _create(cfg, key, **spec_kwargs):
    _write_spec(cfg.home, key, **spec_kwargs)
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": spec_kwargs.get("title", "Test ticket"), "source": "test"}, actor="d")
    return snap_mod.rebuild(cfg.home, key)


# ---------------------------------------------------------------------------
# AcQaVerdict event + record_qa_verdict / qa-verdict verb
# ---------------------------------------------------------------------------

def test_record_qa_verdict_appends_event_and_updates_snapshot(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget", "document it"))
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    h = ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff shows widget.py builds; ran test_widget green")
    snap = snap_mod.load(home, "T-1")
    assert snap.qa_verdicts[h]["verdict"] == "pass"

    ops.record_qa_verdict(cfg, "T-1", 2, "fail", "README.md was not touched by the diff")
    snap = snap_mod.load(home, "T-1")
    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    assert snap.qa_failing_acs(spec_text) == ["document it"]


def test_qa_verdict_via_real_cli(cfg):
    """CLI-driven, per the QA convention: exercise the actual `maestro` surface."""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "fail", "--evidence", "diff has no widget.py at all"])
    assert rc == 0

    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert len(qa_events) == 1
    assert qa_events[0]["payload"]["verdict"] == "fail"
    assert qa_events[0]["payload"]["evidence"] == "diff has no widget.py at all"
    assert qa_events[0]["payload"]["ac_index"] == 1


def test_qa_verdict_rejects_unknown_verdict_value(cfg):
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")
    with pytest.raises(store.MaestroError, match="verdict"):
        ops.record_qa_verdict(cfg, "T-1", 1, "maybe", "n/a")


def test_qa_verdict_out_of_range_raises(cfg):
    _create(cfg, "T-1", acs=("only one thing",))
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")
    with pytest.raises(store.MaestroError, match="out of range"):
        ops.record_qa_verdict(cfg, "T-1", 2, "pass", "n/a")


def test_qa_verdict_re_recorded_after_a_fix_is_not_deduped(cfg):
    """Unlike verify_ac's self-attestation, a re-check of the *same* AC after a
    fix must land as a new event — the step id folds in observed_seq."""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "missing edge case")
    # Something else happens in between (the implementer's fix breadcrumb),
    # advancing observed_seq — this is what a real fix-and-retry round does.
    event_log.append(home, "T-1", "ImplTurnRecorded", {"turn": 2, "role": "implementer"}, actor="reconciler")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "edge case now handled, re-ran tests")

    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert len(qa_events) == 2
    assert [e["payload"]["verdict"] for e in qa_events] == ["fail", "pass"]

    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    snap = snap_mod.load(home, "T-1")
    assert snap.qa_failing_acs(spec_text) == []  # latest verdict wins


# ---------------------------------------------------------------------------
# Enforced gate: a failing QA verdict blocks implementing -> awaiting-ci
# ---------------------------------------------------------------------------

def test_failing_qa_verdict_blocks_awaiting_ci(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "diff does not build the widget")

    with pytest.raises(store.MaestroError, match="refusing awaiting-ci"):
        ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")

    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.QA.value  # never advanced


def test_passing_qa_verdict_after_fix_unblocks_awaiting_ci(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "diff does not build the widget")
    event_log.append(home, "T-1", "ImplTurnRecorded", {"turn": 2, "role": "implementer"}, actor="reconciler")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "widget.py now present and tested")

    # AD-3's separate unverified-AC gate also has to clear before awaiting-ci.
    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value


def test_no_qa_verdicts_recorded_blocks_awaiting_ci_by_default(cfg):
    """T-85: zero verdicts on a current AC used to satisfy the gate silently
    (the bug this ticket closes) -- by default (`awaiting_ci_qa_gate=True`) it
    no longer does, even with the AC self-attested via `verify-ac`. (ACs are
    still verify_ac'd here so this test isolates the QA-completeness gate from
    AD-3's separate unverified-AC gate, which blocks for its own reason.)"""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})
    with pytest.raises(store.MaestroError, match="refusing awaiting-ci"):
        ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value  # never advanced


def test_no_qa_verdicts_recorded_does_not_block_with_gate_off(cfg):
    """T-85 AC4: `awaiting_ci_qa_gate = false` reverts byte-identically to HEAD
    -- a ticket that never ran the QA loop is unaffected by the completeness
    check (only a recorded *fail*, via `_refuse_if_qa_failing`, still gates)."""
    home = cfg.home
    cfg.awaiting_ci_qa_gate = False
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# Skill text: the loop is documented, and the CLI verbs it depends on are real
# ---------------------------------------------------------------------------

def test_reconcile_skill_documents_the_qa_handoff():
    """RF-7: `implementing` hands the adversarial check off to the independent
    `qa` phase (`set-phase qa`) instead of spawning an in-session `Agent`-tool
    QA sub-agent -- that fan-out now lives only in `maestro-reconcile-qa.md`
    (see test_reconcile_skill.py's mirror-consistency checks)."""
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    skill = (repo_root / "skills" / "maestro-reconcile-implementing.md").read_text(encoding="utf-8")
    mirror = (repo_root / ".claude" / "commands" / "maestro-reconcile-implementing.md").read_text(encoding="utf-8")

    for text in (skill, mirror):
        assert "QA" in text
        frontmatter = text.split("---")[1]  # allowed-tools grant (commands copy only)
        assert "Agent" not in frontmatter  # no in-session sub-agent spawn here anymore
        assert 'set-phase "$KEY" qa' in text
        assert "independently" in text or "independent" in text
        # A failing verdict (recorded by `qa`) routes the ticket back to
        # implementing; a fresh hand-off routes onward via `qa`, not straight
        # to awaiting-ci.
        assert "awaiting-ci" in text and "implementing" in text
        assert "fail" in text.lower()


def test_reconcile_skill_qa_documents_the_standards_axis_handoff():
    """RF-7 moved the Standards-axis (T-23) sub-agent spawn from `implementing`
    into `qa` -- the one legal fan-out `qa`'s phase weight prices in."""
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    skill = (repo_root / "skills" / "maestro-reconcile-qa.md").read_text(encoding="utf-8")
    mirror = (repo_root / ".claude" / "commands" / "maestro-reconcile-qa.md").read_text(encoding="utf-8")

    for text in (skill, mirror):
        assert "Agent" in text
        assert "qa_standards_axis" in text or "QA_STANDARDS_AXIS" in text
        assert "--axis standards" in text
        assert "git diff origin/" not in text  # AC6: no re-derived, base-tip-polluted diff


def test_qa_verdict_verb_exists_in_the_real_cli():
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        # Fall back: run the verb's --help through the real argparse entrypoint.
        with pytest.raises(SystemExit) as exc:
            cli.main(["qa-verdict", "--help"])
        assert exc.value.code == 0
    else:
        sub = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
        assert "qa-verdict" in sub
