"""RB-15: `max_impl_turns` is self-reported (a session that never calls
`maestro impl-turn` binds nothing -- measured 2026-08-15: turn 2 by that
counter, 191 raw model turns, 61.1M input tokens). This file covers the real
bound: `max_session_turns` (native spawn-time cap -- claude `--max-turns`,
opencode's generated `--agent` stub `steps:` field) plus
`max_turn_wallclock_seconds` (the dispatcher-side backstop `run_watchdog`
enforces regardless of what the runner does with -- or whether it even has --
a native cap).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import claims, dispatcher as disp, event_log, ops, skills_install, store
from maestro import snapshot as snap_mod
from maestro.config import Config, DEFAULT_CONFIG_TOML
from maestro.sessions import ClaudeCliSessions, DryRunSessions, RoutingSessions
from maestro.statemachine import Phase

from test_pi_sessions import _install_stub_pi


def _seed(home, key, phase=Phase.IMPLEMENTING):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


# ---------------------------------------------------------------------------
# AC2 (native cap) + AC5 (byte-identical default) -- claude's --max-turns
# ---------------------------------------------------------------------------

def _capture_cmd(home, max_session_turns=0):
    sess = ClaudeCliSessions(home=home, capture_session_logs=False,
                             max_session_turns=max_session_turns)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_cmd = []
    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home)
    return captured_cmd


def test_claude_spawn_carries_max_turns_when_configured(home):
    cmd = _capture_cmd(home, max_session_turns=50)
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "50"


def test_claude_spawn_omits_max_turns_flag_by_default(home):
    """AC5/AC7: 0 (the default) is byte-identical to before this ticket -- no
    --max-turns flag at all, not even --max-turns 0."""
    cmd = _capture_cmd(home, max_session_turns=0)
    assert "--max-turns" not in cmd


# ---------------------------------------------------------------------------
# AC2 (native cap) -- opencode's generated --agent stub `steps:` field
# ---------------------------------------------------------------------------

def test_opencode_agent_stub_carries_steps_when_configured():
    stub = skills_install._opencode_agent_stub("a phase", steps=50)
    assert "steps: 50" in stub
    assert "mode: primary" in stub


def test_opencode_agent_stub_omits_steps_by_default():
    """AC5/AC7: None/0 is byte-identical to the pre-RB-15 stub."""
    stub = skills_install._opencode_agent_stub("a phase")
    assert "steps:" not in stub
    assert stub == "---\ndescription: a phase\nmode: primary\n---\n"


def test_install_repo_bakes_configured_turn_cap_into_agent_stubs(home, tmp_path):
    repo = tmp_path / "acme"
    store.atomic_write(home / "config.toml",
                       f'[maestro]\nmax_session_turns = 75\nrunner_enabled = ["claude", "opencode"]\n'
                       f'\n[repos.acme]\npath = "{repo}"\n')
    from maestro import config as config_mod
    from maestro.cli import main as cli_main
    assert cli_main(["--home", str(home), "install-commands", "--repo", "acme"]) == 0
    agent_dir = repo / ".opencode" / "agent"
    one = (agent_dir / "maestro-reconcile-implementing.md").read_text()
    assert "steps: 75" in one


# ---------------------------------------------------------------------------
# AC7: unset/zero -> unbounded, matching min_spawn_interval/test_command
# ---------------------------------------------------------------------------

def test_config_parses_turn_budget_knobs(home):
    from maestro import config as config_mod
    store.atomic_write(home / "config.toml",
                       "[maestro]\nmax_session_turns = 250\nmax_turn_wallclock_seconds = 900\n")
    cfg = config_mod.load(str(home))
    assert cfg.max_session_turns == 250
    assert cfg.max_turn_wallclock_seconds == 900


def test_turn_budget_knobs_default_to_unbounded(home):
    from maestro import config as config_mod
    cfg = config_mod.load(str(home))
    assert cfg.max_session_turns == 0
    assert cfg.max_turn_wallclock_seconds == 0


def test_turn_budget_knobs_documented_in_sample_config():
    assert "max_session_turns" in DEFAULT_CONFIG_TOML
    assert "max_turn_wallclock_seconds" in DEFAULT_CONFIG_TOML


# ---------------------------------------------------------------------------
# AC1/AC3: a session that never calls `impl-turn` (or anything else) is still
# bounded -- a REAL process that loops indefinitely, terminated at the
# configured wall-clock ceiling, proven over real dispatch() sweeps. A fresh
# Failed event names the cutoff and the ticket stays in `implementing` (an
# ACTIVE phase) for a later reconciler to resume, not dead-lettered.
# ---------------------------------------------------------------------------

def _age_claim(home, key, epoch):
    data = claims.read_claim(home, key)
    data["epoch"] = epoch
    store.write_json(claims.claim_path(home, key), data)


def test_watchdog_turn_wallclock_backstop_kills_real_looping_process(home, cfg):
    """AC1/AC3: proven the same way T-13's max_session_seconds/no_output_timeout
    watchdog tests are -- a REAL process group, actually SIGTERM'd by pgid, no
    dependency on the runner ever reporting a turn count of its own."""
    cfg.max_turn_wallclock_seconds = 100
    cfg.max_session_seconds = 0  # isolate: only the turn-budget clock can fire
    _seed(home, "T-1", Phase.IMPLEMENTING)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        claims.write_claim(home, "T-1", proc.pid, "reconcile-T-1")
        _age_claim(home, "T-1", store.now_epoch() - 10_000)  # far past the backstop

        reaped = disp.run_watchdog(cfg, now=store.now_epoch())

        assert reaped == ["T-1"]
        assert claims.read_claim(home, "T-1") is None
        assert not claims.is_claimed(home, "T-1")

        for _ in range(30):  # process group actually died
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None

        events = event_log.read(home, "T-1")
        failed = [e for e in events if e["type"] == "Failed"]
        assert failed, "no Failed event recorded the cutoff"
        assert "turn budget" in failed[-1]["payload"]["error"]
        assert "100s" in failed[-1]["payload"]["error"]

        # AC3: not dead-lettered -- a later reconciler can resume from here.
        assert all(e["type"] != "Stalled" for e in events)
        snap = snap_mod.load(home, "T-1")
        assert snap.phase == "implementing"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_dispatch_sweep_reaps_turn_wallclock_backstop(home, cfg):
    """AC1: 'proven over real dispatch() sweeps', not just a direct
    run_watchdog call -- the exact shape T-13's own
    test_dispatch_runs_watchdog_before_computing_active uses."""
    cfg.max_turn_wallclock_seconds = 100
    cfg.max_session_seconds = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        claims.write_claim(home, "T-1", proc.pid, "reconcile-T-1")
        _age_claim(home, "T-1", store.now_epoch() - 10_000)

        report = disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())

        assert "T-1" in report.reaped
        assert claims.read_claim(home, "T-1") is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_watchdog_turn_wallclock_disabled_when_zero(home, cfg):
    """AC7: 0 (default) disables the backstop entirely -- an existing home
    with nothing configured never reaps on this clock."""
    cfg.max_turn_wallclock_seconds = 0
    cfg.max_session_seconds = 0
    cfg.no_output_timeout = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 1_000_000)  # ancient, but the knob is off

    assert disp.run_watchdog(cfg, now=store.now_epoch()) == []
    assert claims.read_claim(home, "T-1") is not None


def test_watchdog_turn_wallclock_independent_of_max_session_seconds(home, cfg):
    """A session under max_session_seconds but past the (shorter)
    turn-budget backstop is still reaped -- the two clocks are independent,
    same posture as no_output_timeout/max_session_seconds."""
    cfg.max_turn_wallclock_seconds = 100
    cfg.max_session_seconds = 100_000  # far larger -- isolates the turn-budget rule
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")
    _age_claim(home, "T-1", store.now_epoch() - 200)

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == ["T-1"]


def test_watchdog_leaves_session_under_turn_wallclock_untouched(home, cfg):
    cfg.max_turn_wallclock_seconds = 3600
    cfg.max_session_seconds = 0
    _seed(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "reconcile-T-1")  # young, this process: alive

    reaped = disp.run_watchdog(cfg, now=store.now_epoch())

    assert reaped == []
    assert claims.is_claimed(home, "T-1")
    events = event_log.read(home, "T-1")
    assert all(e["type"] != "Failed" for e in events)


# ---------------------------------------------------------------------------
# AC2 (pi backstop): pi has NO native turn cap of its own -- a real pi spawn
# is protected ENTIRELY by the wall-clock backstop above.
# ---------------------------------------------------------------------------

def test_run_watchdog_turn_wallclock_reaps_a_real_pi_spawn_with_no_native_cap(
        home, cfg, tmp_path, monkeypatch):
    from maestro.sessions import PiCliSessions

    _install_stub_pi(tmp_path, monkeypatch)
    sess = PiCliSessions(home=home, capture_session_logs=False)
    _seed(home, "T-1", Phase.IMPLEMENTING)

    cfg.max_turn_wallclock_seconds = 100
    cfg.max_session_seconds = 0
    pid = sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home, runner_model="glm-5.2")
    try:
        c = claims.read_claim(home, "T-1")
        c["epoch"] = store.now_epoch() - 10_000
        store.write_json(claims.claim_path(home, "T-1"), c)

        reaped = disp.run_watchdog(cfg, now=store.now_epoch())

        assert reaped == ["T-1"]
        assert claims.read_claim(home, "T-1") is None

        reaped_pid = 0
        for _ in range(30):
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            if reaped_pid == pid:
                break
            time.sleep(0.1)
        assert reaped_pid == pid, "stub pi process was not reaped by the watchdog's SIGTERM"
    finally:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


# ---------------------------------------------------------------------------
# AC4: T-68's Checked no-op vs crash distinction is unweakened by the new
# clock -- a genuine no-op still advances observed_seq via Checked, entirely
# independent of the watchdog kill path above.
# ---------------------------------------------------------------------------

def test_checked_no_op_unaffected_by_turn_budget_knobs(home, cfg):
    cfg.max_turn_wallclock_seconds = 100
    _seed(home, "T-1", Phase.IMPLEMENTING)
    before = snap_mod.load(home, "T-1").observed_seq
    ops.checked(cfg, "T-1")
    after = snap_mod.load(home, "T-1").observed_seq
    assert after == before + 1
    events = event_log.read(home, "T-1")
    assert events[-1]["type"] == "Checked"
