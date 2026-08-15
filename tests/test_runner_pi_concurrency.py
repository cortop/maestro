"""PI-8 AC8: the per-runner concurrency cap (`[runner.pi]`'s `concurrency`
key, default 1 -- see `dispatcher._RUNNER_DEFAULT_CONCURRENCY`'s PI-8
comment). Enforced inside the preflight block, BEFORE `_allow_spawn` -- a
capped-out ticket costs no attempts-ledger spend, same "group-level skip"
shape `test_runner_opencode_concurrency.py` already proves for opencode.
"""
from __future__ import annotations

import os
import time

from maestro import claims, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.sessions import ClaudeCliSessions, DryRunSessions, PiCliSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_preflight import _counting_probe, _seed

RUNNER = "pi"

_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"model": "glm-5.2"}],
    "daemon_reason": None,
})


def _configure(cfg, concurrency, *, phases=("implementing",)):
    cfg.provider_config = {"runner": {"pi": {"concurrency": concurrency, "phases": list(phases)}}}


# --- AC8: cap at 1, two due pi tickets -> exactly one spawns, no ledger spend --

def test_cap_at_one_with_two_due_pi_tickets_spawns_exactly_one(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _configure(cfg, 1)
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert len(sessions.spawned) == 1
    spawned_key = sessions.spawned[0][0]
    other = "PI-2" if spawned_key == "PI-1" else "PI-1"
    assert other not in report.spawned

    decision = disp.key_decisions(home, other, tail=1)[0]
    assert decision["outcome"] == "runner_capped"
    events = event_log.read(home, other)
    assert len(events) == 2  # just the seeded TicketCreated + PhaseChanged
    attempts = store.read_json(home / "derived" / ".spawn_attempts.json", {}) or {}
    assert other not in attempts


def test_cap_at_two_with_two_due_pi_tickets_spawns_both(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _configure(cfg, 2)
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert set(report.spawned) == {"PI-1", "PI-2"}


def test_cap_never_throttles_the_claude_sibling(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _configure(cfg, 1)
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "CL-1", phase=Phase.READY)
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "CL-1" in report.spawned
    assert len([k for k in report.spawned if k != "CL-1"]) == 1


def test_default_cap_for_pi_is_one_when_unconfigured(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    # phases must still be admitted explicitly (pi's own empty default) --
    # concurrency itself is left unset to prove the _RUNNER_DEFAULT_CONCURRENCY
    # fallback (1), not an explicit config value.
    cfg.provider_config = {"runner": {"pi": {"phases": ["implementing"]}}}
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    sessions = DryRunSessions()

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert len([k for k in report.spawned if k in ("PI-1", "PI-2")]) == 1


# --- the cap counts from the claim written at spawn time, not a re-resolved phase --

def test_cap_counts_a_stale_phase_claim_not_a_reresolved_phase(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    _configure(cfg, 1)
    _seed(home, "PI-1", phase=Phase.QA)  # folded past pi's admitted phase
    claims.write_claim(home, "PI-1", 999999, "reconcile-PI-1", runner=RUNNER)
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    sessions = DryRunSessions(active={"PI-1"})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)

    assert "PI-2" not in report.spawned
    decision = disp.key_decisions(home, "PI-2", tail=1)[0]
    assert decision["outcome"] == "runner_capped"


# --- AC6: a fast-exiting REAL stub bounded across TEN consecutive real sweeps,
# by the PREFLIGHT cap -- not `_allow_spawn`'s max_spawn_attempts circuit
# breaker (spec Notes: a stub that exits in under a second leaves no claim, so
# the naive "count active claims" form of this AC is unsatisfiable; the
# bounding must come from the concurrency-cap preflight check instead) --------

_STUB_PI_FAST_EXIT = "#!/bin/sh\nexit 0\n"


def _install_fast_exit_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    stub = bin_dir / "pi"
    stub.write_text(_STUB_PI_FAST_EXIT)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def test_fast_exiting_stub_bounded_by_the_cap_across_ten_consecutive_real_sweeps(
        home, cfg, tmp_path, monkeypatch):
    _install_fast_exit_stub(tmp_path, monkeypatch)
    cfg.runner_enabled = ["claude", RUNNER]
    cfg.max_spawn_attempts = 50  # deliberately generous -- proves the CAP is what
                                  # bounds this, not the no-progress circuit breaker
    _configure(cfg, 1)
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")
    _seed(home, "PI-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="glm-5.2")

    sessions = RoutingSessions({
        "claude": ClaudeCliSessions(home),
        "pi": PiCliSessions(home, capture_session_logs=False),
    })

    for i in range(10):
        report = disp.dispatch(cfg, sessions, now=1000 + i * 10, runner_probe=_TOOL_CAPABLE_PROBE)
        pi_spawns = [k for k in report.spawned if k in ("PI-1", "PI-2")]
        assert len(pi_spawns) <= 1, f"sweep {i}: cap exceeded: {pi_spawns}"
        time.sleep(0.2)  # let the fast-exiting stub actually exit before the next sweep

    for key in ("PI-1", "PI-2"):
        assert snap_mod.load(home, key).phase == Phase.IMPLEMENTING.value
        assert not any(e["type"] == "Failed" for e in event_log.read(home, key))
