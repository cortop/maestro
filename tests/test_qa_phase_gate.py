"""T-85: two write-path preconditions close the gap left by RB-16's spawn-time
verb denylist (a runner permission, not an invariant) and RF-7's move of the
adversarial review into its own `qa` phase (which nothing actually required a
ticket to ENTER before `awaiting-ci`):

1. `ops.record_qa_verdict` refuses (raises `store.MaestroError`, appends NO
   event) unless the ticket's folded phase is `qa` -- config-gated by
   `qa_phase_gate` (default ON).
2. `ops.set_phase(..., Phase.AWAITING_CI)` refuses unless every current-hash
   AC carries a PASSING spec-axis QA verdict, not merely no failing one --
   config-gated by `awaiting_ci_qa_gate` (default ON).

Both gates are ships-dark-when-off ablatable, matching the `qa_standards_axis`/
`repo_preflight` boolean-knob convention (this board predates the
`test_deletion_gate`/`stub_markers` knobs the spec's own Notes reference --
those live only on an unmerged branch; the convention is the same either way).
"""
from __future__ import annotations

import sys

from maestro import cli, config as config_mod, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import make_origin_and_repo

SPEC_TEMPLATE = """\
# {key}: {title}

priority: 2

## Intent
{intent}

## Acceptance criteria
{acs}
"""

_PASS_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'


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


# ---------------------------------------------------------------------------
# AC1/AC2: `qa-verdict` refuses outside the `qa` phase, no event appended; the
# identical call in `qa` still exits 0 and appends exactly one event -- the
# happy path is byte-identical to HEAD.
# ---------------------------------------------------------------------------

def test_qa_verdict_refuses_outside_qa_phase_via_real_cli(home, cfg):
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    before = event_log.read(home, "T-1")

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "pass", "--evidence", "x"])

    assert rc != 0
    after = event_log.read(home, "T-1")
    assert after == before  # zero AcQaVerdict events appended
    assert not [e for e in after if e["type"] == "AcQaVerdict"]


def test_qa_verdict_succeeds_in_qa_phase_byte_identical_happy_path(home, cfg):
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "pass", "--evidence", "x"])

    assert rc == 0
    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert len(qa_events) == 1
    assert qa_events[0]["payload"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# AC3: `awaiting-ci` refuses when a current AC has no PASSING spec-axis QA
# verdict -- self-attestation via `verify-ac` alone does not satisfy it -- and
# succeeds once a qa-phase `qa-verdict --verdict pass` exists for each AC.
# ---------------------------------------------------------------------------

def test_awaiting_ci_refuses_on_self_attestation_alone_then_succeeds_with_qa_pass(home, cfg):
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})
    before = event_log.read(home, "T-1")

    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "tests green"])

    assert rc != 0
    after = event_log.read(home, "T-1")
    assert after == before  # no new PhaseChanged
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value

    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")
    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "pass", "--evidence", "widget.py builds; tests green"])
    assert rc == 0

    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC4: both gates are per-board ablatable, config.toml knobs defaulting ON --
# with each OFF, the flows above behave byte-identically to HEAD.
# ---------------------------------------------------------------------------

def test_qa_phase_gate_defaults_on(cfg):
    assert cfg.qa_phase_gate is True


def test_awaiting_ci_qa_gate_defaults_on(cfg):
    assert cfg.awaiting_ci_qa_gate is True


def test_config_toml_can_disable_both_knobs(home):
    (home / "config.toml").write_text(
        "[maestro]\nqa_phase_gate = false\nawaiting_ci_qa_gate = false\n")
    loaded = config_mod.load(str(home))
    assert loaded.qa_phase_gate is False
    assert loaded.awaiting_ci_qa_gate is False


def test_qa_phase_gate_off_reverts_to_head_a_verdict_honored_from_any_phase(home, cfg):
    cfg.qa_phase_gate = False
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    h = ops.record_qa_verdict(cfg, "T-1", 1, "pass", "x")

    snap = snap_mod.load(home, "T-1")
    assert snap.qa_verdicts[h]["verdict"] == "pass"


def test_awaiting_ci_qa_gate_off_reverts_to_head_self_attestation_is_enough(home, cfg):
    cfg.awaiting_ci_qa_gate = False
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})

    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")  # no QA verdict at all

    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC6: a real dispatcher sweep still completes the full loop -- verifying ->
# qa -> awaiting-ci, with a passing verdict recorded, and no ticket wedged.
# ---------------------------------------------------------------------------

def test_real_dispatcher_sweep_completes_verifying_qa_awaiting_ci_loop(tmp_path, home):
    import os
    from maestro import claims

    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), branch_prefix="maestro/", test_command=_PASS_CMD)

    ac_lines = "- [ ] do the thing"
    store.atomic_write(store.spec_path(home, "G-1"),
                       f"# G-1: t\npriority: 2\n\n## Intent\nx\n\n## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, "G-1", "TicketCreated",
                     {"title": "G-1", "source": "test", "spec_hash": disp.spec_hash_on_disk(home, "G-1")},
                     actor="d")
    snap_mod.rebuild(home, "G-1")
    ops.set_phase(cfg, "G-1", Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, "G-1")
    ops.set_phase(cfg, "G-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.verify_ac(cfg, "G-1", 1, {"what": "ran pytest", "where": "tests/x.py", "result": "PASSED"})
    ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")  # redirected to verifying
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)  # starts the real test_command subprocess
    claim = claims.read_claim(home, "G-1")
    for _ in range(200):  # bounded poll, never a foreground blocking sleep
        if not claims.pid_alive(claim["pid"]):
            break
        os.sched_yield()
    else:
        os.waitpid(claim["pid"], 0)
    disp.dispatch(cfg, sessions, now=1001)  # folds the green result, routes to qa, spawns it
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value

    # The independent `qa` reconciler's own step: record a passing verdict,
    # hand off to awaiting-ci.
    rc = cli.main(["--home", str(home), "qa-verdict", "G-1", "--ac", "1",
                   "--verdict", "pass", "--evidence", "diff builds the thing; tests green"])
    assert rc == 0
    rc = cli.main(["--home", str(home), "set-phase", "G-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0
    assert snap_mod.load(home, "G-1").phase == Phase.AWAITING_CI.value

    # Nothing wedged: a further sweep neither raises nor re-spawns implementing/qa.
    report = disp.dispatch(cfg, sessions, now=1002)
    assert "G-1" not in report.spawned


# ---------------------------------------------------------------------------
# AC7: the spawn-layer grant/denylist and the write-layer gate agree -- one
# does not replace the other.
# ---------------------------------------------------------------------------

def test_spawn_layer_denylist_and_grant_still_cover_qa_verdict():
    assert "Bash(maestro qa-verdict:*)" in disp.phase_verb_denylist("implementing")
    assert "Bash(maestro qa-verdict:*)" in disp.phase_verb_grant("qa")
