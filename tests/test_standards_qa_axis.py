"""T-23: a second, config-gated "standards" QA axis alongside AD-4's "spec" axis.

A change can pass one axis and fail the other (right feature, wrong conventions
vs. right conventions, wrong feature). This adds `maestro qa-verdict --axis
standards`, kept in a separate snapshot bucket from the spec axis (never
reranked against it), config-gated by `qa_standards_axis` (default OFF), and
-- an explicit, tested choice -- advisory only: unlike a spec-axis fail, a
standards-axis fail does NOT block `implementing -> awaiting-ci`.
"""
import pytest

from maestro import cli, config as config_mod, event_log, ops, snapshot as snap_mod, store
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
# AC1: config.toml gains qa_standards_axis, defaulting OFF
# ---------------------------------------------------------------------------

def test_qa_standards_axis_defaults_off(home):
    cfg = config_mod.load(str(home))
    assert cfg.qa_standards_axis is False


def test_qa_standards_axis_can_be_enabled_via_config(home):
    (home / "config.toml").write_text("[maestro]\nqa_standards_axis = true\n")
    cfg = config_mod.load(str(home))
    assert cfg.qa_standards_axis is True


def test_maestro_env_surfaces_qa_standards_axis(home, capsys):
    (home / "config.toml").write_text("[maestro]\nqa_standards_axis = true\n")
    assert cli.main(["--home", str(home), "env"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["qa_standards_axis"] is True


# ---------------------------------------------------------------------------
# AC3: `maestro qa-verdict` supports `--axis standards`, recorded separately
# from the spec axis (never reranked against it)
# ---------------------------------------------------------------------------

def test_record_qa_verdict_defaults_to_spec_axis(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    h = ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff builds the widget")
    snap = snap_mod.load(home, "T-1")
    assert snap.qa_verdicts[h]["verdict"] == "pass"
    assert snap.qa_verdicts_standards == {}


def test_record_qa_verdict_standards_axis_lands_in_a_separate_bucket(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))

    # Right feature, wrong conventions: spec axis passes, standards axis fails.
    h_spec = ops.record_qa_verdict(cfg, "T-1", 1, "pass", "widget.py builds and works",
                                   axis="spec")
    h_std = ops.record_qa_verdict(cfg, "T-1", 1, "fail", "mocks the component under test; "
                                  "violates CLAUDE.md's real-app QA rule", axis="standards")
    assert h_spec == h_std  # same AC -> same content hash, different buckets

    snap = snap_mod.load(home, "T-1")
    assert snap.qa_verdicts[h_spec]["verdict"] == "pass"
    assert snap.qa_verdicts_standards[h_std]["verdict"] == "fail"

    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    # Never reranked / merged: the spec axis is unaffected by the standards fail.
    assert snap.qa_failing_acs(spec_text) == []
    assert snap.standards_failing_acs(spec_text) == ["build the widget"]


def test_qa_verdict_axis_via_real_cli(cfg):
    """CLI-driven, per the QA convention: exercise the actual `maestro` surface."""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "fail", "--axis", "standards",
                   "--evidence", "one-line docstring missing from new module"])
    assert rc == 0

    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert len(qa_events) == 1
    assert qa_events[0]["payload"]["axis"] == "standards"
    assert qa_events[0]["payload"]["verdict"] == "fail"

    snap = snap_mod.load(home, "T-1")
    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    assert snap.standards_failing_acs(spec_text) == ["build the widget"]
    assert snap.qa_failing_acs(spec_text) == []  # spec axis untouched


def test_qa_verdict_rejects_unknown_axis(cfg):
    _create(cfg, "T-1", acs=("build the widget",))
    with pytest.raises(store.MaestroError, match="axis"):
        ops.record_qa_verdict(cfg, "T-1", 1, "pass", "n/a", axis="vibes")


def test_qa_verdict_cli_rejects_unknown_axis(cfg):
    """argparse's own --choices restriction rejects a bogus --axis before it
    ever reaches ops.record_qa_verdict -- belt-and-suspenders with the ops-level
    validation in test_qa_verdict_rejects_unknown_axis above."""
    _create(cfg, "T-1", acs=("build the widget",))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--home", str(cfg.home), "qa-verdict", "T-1", "--ac", "1",
                  "--verdict", "pass", "--axis", "vibes", "--evidence", "n/a"])
    assert exc.value.code != 0


def test_pre_t23_events_without_axis_field_fold_as_spec_axis(cfg):
    """Backward compat: an AcQaVerdict event recorded before T-23 has no `axis`
    key at all -- it must fold into the spec-axis bucket (unchanged gating),
    not silently disappear or land in the standards bucket."""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    h = snap_mod.ac_hash("build the widget")
    event_log.append(home, "T-1", "AcQaVerdict",
                     {"ac_hash": h, "ac_index": 1, "ac_text": "build the widget",
                      "verdict": "fail", "evidence": "pre-T-23 event, no axis field"},
                     actor="reconciler-qa")
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.qa_verdicts[h]["verdict"] == "fail"
    assert snap.qa_verdicts_standards == {}


# ---------------------------------------------------------------------------
# AC4/AC5: the standards axis's effect on awaiting-ci is explicit -- it does
# NOT block, unlike the spec axis -- and proven via the real CLI / snapshot.
# ---------------------------------------------------------------------------

def test_standards_axis_fail_does_not_block_awaiting_ci(cfg):
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff builds the widget", axis="spec")
    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "mocks the real app", axis="standards")
    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})

    # Would raise if the standards axis were consulted by the gate -- it isn't.
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value

    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    # The failure is still visible for a human reviewer, just non-blocking.
    assert snap.standards_failing_acs(spec_text) == ["build the widget"]


def test_spec_axis_fail_still_blocks_awaiting_ci_alongside_standards_axis(cfg):
    """The spec axis keeps its exact current (AD-1/AD-3/AD-4) meaning -- adding
    the standards axis must not weaken it."""
    home = cfg.home
    _create(cfg, "T-1", acs=("build the widget",))
    ops.set_phase(cfg, "T-1", Phase.READY, reason="approved")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "widget.py missing", axis="spec")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "conventions look fine", axis="standards")

    with pytest.raises(store.MaestroError, match="refusing awaiting-ci"):
        ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.IMPLEMENTING.value


def test_standards_axis_gate_choice_is_documented_in_the_skill():
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    skill = (repo_root / "skills" / "maestro-reconcile.md").read_text(encoding="utf-8")
    mirror = (repo_root / ".claude" / "commands" / "maestro-reconcile.md").read_text(encoding="utf-8")
    for text in (skill, mirror):
        assert "qa_standards_axis" in text
        assert "--axis standards" in text
        assert "does" in text and "not** block" in text or "does not block" in text.replace("**", "")
        assert "awaiting-ci" in text


# ---------------------------------------------------------------------------
# AC2: config-gated -- the skill only spawns the second agent when enabled
# ---------------------------------------------------------------------------

def test_skill_gates_standards_agent_on_config_flag():
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    for path in (repo_root / "skills" / "maestro-reconcile.md",
                 repo_root / ".claude" / "commands" / "maestro-reconcile.md"):
        text = path.read_text(encoding="utf-8")
        assert "QA_STANDARDS_AXIS" in text
        assert "Fowler" in text
        assert "CLAUDE.md" in text
        assert "parallel" in text.lower()
