"""OC-4 AC1: a real dispatch sweep routes an `implementing` ticket carrying
`runner: opencode` to the opencode arm of a `RoutingSessions`, and every other
ticket (including a `claude`-default ticket) to the claude arm -- proven with
BOTH arms as `DryRunSessions` (no real process spawn; that's
`test_opencode_sessions.py`'s job), over a real `disp.dispatch()` sweep.
"""
from __future__ import annotations

from maestro import dispatcher as disp
from maestro.sessions import DryRunSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_preflight import RUNNER, _counting_probe, _seed

_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"name": "a:1b", "capabilities": ["tools"]}],
    "daemon_reason": None,
})


def test_routing_sends_opencode_ticket_to_opencode_arm_others_to_claude_arm(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "CL-1", phase=Phase.READY)

    claude_arm = DryRunSessions()
    opencode_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "opencode": opencode_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert set(report.spawned) == {"OC-1", "CL-1"}
    assert [s[0] for s in opencode_arm.spawned] == ["OC-1"]
    assert [s[0] for s in claude_arm.spawned] == ["CL-1"]
    # the opencode arm recorded the resolved runner_model too
    assert opencode_arm.spawned[0][-1] == "a:1b"
    assert claude_arm.spawned[0][-1] is None


def test_routing_never_routes_a_non_implementing_opencode_ticket_to_opencode_arm(home, cfg):
    """RF-2's own rule (resolve_runner): every phase besides `implementing`
    always spawns claude, regardless of the spec's `runner:` line."""
    cfg.runner_enabled = ["claude", RUNNER]
    _seed(home, "OC-1", phase=Phase.TRIAGING, runner=RUNNER, runner_model="a:1b")

    claude_arm = DryRunSessions()
    opencode_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "opencode": opencode_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "OC-1" in report.spawned
    assert opencode_arm.spawned == []
    assert [s[0] for s in claude_arm.spawned] == ["OC-1"]
