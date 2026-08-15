"""PI-8 AC7: a real dispatch sweep routes a ticket carrying `runner: pi` to the
pi arm of a `RoutingSessions` -- ONLY on a phase `[runner.pi] phases` admits it
to (pi's own default eligible-phase set is empty, unlike opencode's
`["implementing"]` -- see `dispatcher._RUNNER_DEFAULT_PHASES`'s PI-8 comment)
-- and every other ticket (including a `claude`-default ticket, and the SAME
ticket's own `implementing` spawn) to the claude arm. Both arms are
`DryRunSessions` (no real process spawn; that's `test_pi_sessions.py`'s job).
"""
from __future__ import annotations

from maestro import dispatcher as disp, event_log, snapshot as snap_mod
from maestro.sessions import DryRunSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_preflight import _counting_probe, _seed

RUNNER = "pi"

# pi's own verdict (`providers.pi.verdict_for_model`) reads the `model` key,
# not `name`/`capabilities` (the ollama shape `test_runner_preflight`'s own
# module-level probe uses) -- see `providers.pi.model_names`.
_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"model": "glm-5.2"}],
    "daemon_reason": None,
})


def _admit(cfg, *phases):
    cfg.provider_config = {"runner": {"pi": {"phases": list(phases)}}}


def test_routing_sends_pi_ticket_to_pi_arm_on_its_permitted_phase_others_to_claude_arm(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _admit(cfg, "researching")
    _seed(home, "PI-1", phase=Phase.RESEARCHING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "CL-1", phase=Phase.READY)

    claude_arm = DryRunSessions()
    pi_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "pi": pi_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert set(report.spawned) == {"PI-1", "CL-1"}
    assert [s[0] for s in pi_arm.spawned] == ["PI-1"]
    assert [s[0] for s in claude_arm.spawned] == ["CL-1"]
    # the pi arm recorded the resolved runner_model too
    assert pi_arm.spawned[0][-1] == "glm-5.2"
    assert claude_arm.spawned[0][-1] is None


def test_routing_never_routes_an_unadmitted_phase_pi_ticket_to_the_pi_arm(home, cfg):
    """`resolve_runner`'s own rule: a phase absent from `[runner.pi] phases`
    always spawns claude, regardless of the spec's `runner:` line -- pi's
    empty DEFAULT phase set means this is true for `implementing` even with
    NO config override at all."""
    cfg.runner_enabled = ["claude", RUNNER]
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")

    claude_arm = DryRunSessions()
    pi_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "pi": pi_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "PI-1" in report.spawned
    assert pi_arm.spawned == []
    assert [s[0] for s in claude_arm.spawned] == ["PI-1"]


def test_same_ticket_pi_permitted_phase_goes_to_pi_implementing_goes_to_claude(home, cfg):
    """AC7's own second half: in ONE real flow, the SAME ticket's pi-permitted
    phase spawns via the pi arm, and its later `implementing` spawn (pi not
    admitted there by default) goes to claude."""
    cfg.runner_enabled = ["claude", RUNNER]
    cfg.min_spawn_interval = 0
    _admit(cfg, "researching")
    _seed(home, "T-1", phase=Phase.RESEARCHING, runner=RUNNER, runner_model="glm-5.2")

    claude_arm = DryRunSessions()
    pi_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "pi": pi_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert "T-1" in report.spawned
    assert [s[0] for s in pi_arm.spawned] == ["T-1"]
    assert claude_arm.spawned == []

    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    report2 = disp.dispatch(cfg, sessions, now=2000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert "T-1" in report2.spawned
    assert [s[0] for s in claude_arm.spawned] == ["T-1"]
    assert [s[0] for s in pi_arm.spawned] == ["T-1"]  # unchanged from before
