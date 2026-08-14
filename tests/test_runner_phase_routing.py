"""T-54: `resolve_runner`'s eligible-phase set is now a `Phase`-keyed table
sourced from `[runner.<name>].phases` config (`dispatcher.runner_eligible_phases`),
not a single hardcoded "implementing only" check -- admitting a runner to a
read-mostly phase (`triaging`/`researching`/`qa`) is a config change, not a code
edit. Default behaviour (no `phases` configured) stays "implementing only",
proven byte-identical by `test_runner.py`/`test_runner_opencode_routing.py`
(deliberately left unmodified -- see this file's own Notes).
"""
from __future__ import annotations

from maestro import dispatcher as disp
from maestro.config import Config
from maestro.sessions import DryRunSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_preflight import RUNNER, _counting_probe, _enable, _register, _seed

_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"name": "a:1b", "capabilities": ["tools"]}],
    "daemon_reason": None,
})


# --- AC1: runner_eligible_phases + resolve_runner's Phase-keyed table -------

def test_eligible_phases_defaults_to_implementing_only_when_unconfigured(home):
    cfg = Config(home=home)
    assert disp.runner_eligible_phases(cfg, "opencode") == frozenset({Phase.IMPLEMENTING})


def test_eligible_phases_reads_the_configured_list(home):
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"phases": ["qa", "triaging"]}}})
    assert disp.runner_eligible_phases(cfg, "opencode") == frozenset({Phase.QA, Phase.TRIAGING})


def test_eligible_phases_skips_unparseable_entries_never_raises(home):
    cfg = Config(home=home,
                 provider_config={"runner": {"opencode": {"phases": ["qa", "not-a-real-phase"]}}})
    assert disp.runner_eligible_phases(cfg, "opencode") == frozenset({Phase.QA})


def test_eligible_phases_for_an_unconfigured_unknown_runner_defaults_to_implementing(home):
    cfg = Config(home=home)
    assert disp.runner_eligible_phases(cfg, "some-future-runner") == frozenset({Phase.IMPLEMENTING})


def test_resolve_runner_admits_opencode_to_qa_when_configured(home):
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"phases": ["qa"]}}})
    _seed(home, "T-1", phase=Phase.QA, runner=RUNNER, runner_model="a:1b")
    assert disp.resolve_runner(cfg, "T-1", Phase.QA.value) == (RUNNER, "a:1b")


def test_resolve_runner_still_forces_claude_for_implementing_when_only_qa_admitted(home):
    """AC3: admitting a runner to `qa` doesn't also admit it to `implementing`
    -- each phase is checked against the SAME runner's own eligible set."""
    cfg = Config(home=home, provider_config={"runner": {"opencode": {"phases": ["qa"]}}})
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    assert disp.resolve_runner(cfg, "T-1", Phase.IMPLEMENTING.value) == ("claude", None)


def test_resolve_runner_unparseable_phase_falls_back_to_claude(home):
    cfg = Config(home=home)
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    assert disp.resolve_runner(cfg, "T-1", "not-a-real-phase") == ("claude", None)


# --- AC3: a real sweep routes a qa ticket to the admitted runner's arm, and
# an implementing ticket on the SAME board to claude ------------------------

def test_real_sweep_routes_qa_ticket_to_runner_arm_implementing_ticket_to_claude(
        home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    cfg.provider_config = {"runner": {"opencode": {"phases": ["qa"]}}}
    _seed(home, "QA-1", phase=Phase.QA, runner=RUNNER, runner_model="a:1b")
    _seed(home, "IMPL-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")

    claude_arm = DryRunSessions()
    opencode_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "opencode": opencode_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert set(report.spawned) == {"QA-1", "IMPL-1"}
    assert [s[0] for s in opencode_arm.spawned] == ["QA-1"]
    assert [s[0] for s in claude_arm.spawned] == ["IMPL-1"]
