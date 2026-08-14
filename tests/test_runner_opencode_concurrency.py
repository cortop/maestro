"""OC-4: the per-runner concurrency cap (`[runner.opencode]`'s `concurrency`
key, default 1 -- spec Notes' QW-3 bound). Enforced inside OC-2's own
preflight block (`dispatcher._runner_concurrency_cap`), BEFORE `_allow_spawn`
-- a capped-out ticket costs no attempts-ledger spend and gets no `ops.ask`;
it just stays due and is retried next sweep once a slot frees up, same
"group-level skip" shape `repo_capped` already has.
"""
from __future__ import annotations

import os
import time

from maestro import claims, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.sessions import ClaudeCliSessions, DryRunSessions, OpencodeCliSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_preflight import RUNNER, _counting_probe, _seed

_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"name": "a:1b", "capabilities": ["tools"]}],
    "daemon_reason": None,
})


def _cap(cfg, n):
    cfg.provider_config = {"runner": {"opencode": {"concurrency": n}}}


# --- AC6: cap at 1, two due opencode tickets -> exactly one spawns ----------

def test_cap_at_one_with_two_due_opencode_tickets_spawns_exactly_one(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _cap(cfg, 1)
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert len(sessions.spawned) == 1
    spawned_key = sessions.spawned[0][0]
    other = "OC-2" if spawned_key == "OC-1" else "OC-1"
    assert other not in report.spawned

    decision = disp.key_decisions(home, other, tail=1)[0]
    assert decision["outcome"] == "runner_capped"
    # group-level skip: no event, no attempts spend, ticket stays untouched
    events = event_log.read(home, other)
    assert len(events) == 2  # just the seeded TicketCreated + PhaseChanged
    attempts = store.read_json(home / "derived" / ".spawn_attempts.json", {}) or {}
    assert other not in attempts


def test_cap_at_two_with_two_due_opencode_tickets_spawns_both(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _cap(cfg, 2)
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert set(report.spawned) == {"OC-1", "OC-2"}


def test_cap_never_throttles_the_claude_sibling(home, cfg):
    """The cap is per-runner -- a claude ticket due in the same sweep is
    untouched by opencode's own cap."""
    cfg.runner_enabled = ["claude", RUNNER]
    _cap(cfg, 1)
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "CL-1", phase=Phase.READY)
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "CL-1" in report.spawned
    assert len([k for k in report.spawned if k != "CL-1"]) == 1


def test_default_cap_for_opencode_is_one_when_unconfigured(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    # no [runner.opencode] table at all -- falls back to the measured default
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert len([k for k in report.spawned if k in ("OC-1", "OC-2")]) == 1


# --- AC4: a fast-exiting real stub over a SEQUENCE of real sweeps -----------
# still bounded by the cap (not by `_allow_spawn`'s no-progress circuit
# breaker, which would instead dead-letter a ticket) -------------------------

_STUB_OPENCODE_FAST_EXIT = "#!/bin/sh\nexit 0\n"


def _install_fast_exit_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(_STUB_OPENCODE_FAST_EXIT)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def test_fast_exiting_stub_bounded_by_the_cap_across_a_sequence_of_real_sweeps(
        home, cfg, tmp_path, monkeypatch):
    _install_fast_exit_stub(tmp_path, monkeypatch)
    cfg.runner_enabled = ["claude", RUNNER]
    cfg.max_spawn_attempts = 50  # deliberately generous -- proves the CAP is what
                                  # bounds this, not the no-progress circuit breaker
    _cap(cfg, 1)
    _seed(home, "OC-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")

    sessions = RoutingSessions({
        "claude": ClaudeCliSessions(home),
        "opencode": OpencodeCliSessions(home, capture_session_logs=False),
    })

    for i in range(3):
        report = disp.dispatch(cfg, sessions, now=1000 + i * 10, runner_probe=_TOOL_CAPABLE_PROBE)
        opencode_spawns = [k for k in report.spawned if k in ("OC-1", "OC-2")]
        assert len(opencode_spawns) <= 1, f"sweep {i}: cap exceeded: {opencode_spawns}"
        time.sleep(0.2)  # let the fast-exiting stub actually exit before the next sweep

    # neither ticket got dead-lettered -- the cap did the bounding, not
    # `_allow_spawn`'s max_spawn_attempts circuit breaker.
    for key in ("OC-1", "OC-2"):
        assert snap_mod.load(home, key).phase == Phase.IMPLEMENTING.value
        assert not any(e["type"] == "Failed" for e in event_log.read(home, key))


# --- AC4: the cap counts from the claim written at spawn time, not from a
# re-resolved `resolve_runner(cfg, k, CURRENT phase)` ------------------------

def test_cap_counts_a_stale_phase_claim_not_a_reresolved_phase(home, cfg):
    """T-54's own bug fix: OC-1 was spawned under `implementing` (its claim
    records `runner: opencode`) and has since folded to `qa`, a phase where
    opencode is NOT eligible by default -- re-resolving `resolve_runner(cfg,
    "OC-1", "qa")` would return `claude` and silently stop counting it,
    leaking the cap slot (the exact bug this ticket exists to fix). Reading
    the claim's OWN recorded runner instead still counts it, so a due OC-2
    ticket is refused."""
    cfg.runner_enabled = ["claude", RUNNER]
    _cap(cfg, 1)
    _seed(home, "OC-1", phase=Phase.QA)  # folded past implementing; no runner: override needed
    claims.write_claim(home, "OC-1", os.getpid(), "reconcile-OC-1", runner=RUNNER)
    _seed(home, "OC-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    sessions = DryRunSessions(active={"OC-1"})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "OC-2" not in report.spawned
    decision = disp.key_decisions(home, "OC-2", tail=1)[0]
    assert decision["outcome"] == "runner_capped"
