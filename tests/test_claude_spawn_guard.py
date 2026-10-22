"""T-207: every claude spawn loads the maestro-owned PreToolUse guard, refuses
without it, and the dispatcher interlock checks the runner that spawns."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from maestro import claude_guard, event_log, gates, sessions, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.config import Config
from maestro.sessions import ClaudeCliSessions, DryRunSessions
from maestro.statemachine import Phase


class _Launch:
    def __init__(self):
        self.calls = []

    def __call__(self, home, key, cmd, cwd, env, log_file, **kw):
        self.calls.append(cmd)
        return 4242


@pytest.fixture
def launch(monkeypatch):
    rec = _Launch()
    monkeypatch.setattr(sessions, "_launch_detached", rec)
    return rec


def test_spawn_passes_guard_settings(home, tmp_path, launch):
    ClaudeCliSessions(home).spawn("X-1", "/maestro-reconcile", tmp_path)
    cmd = launch.calls[0]
    assert cmd.count("--settings") == 1
    settings = claude_guard.settings_path(home)
    assert cmd[cmd.index("--settings") + 1] == str(settings)
    hooks = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "Bash"
    script = claude_guard.hook_script_path(home)
    assert str(script) in hooks[0]["hooks"][0]["command"]
    assert script.is_file() and (script.parent / "destructive_command_guard.py").is_file()


def test_installed_hook_blocks_a_destructive_command(home, tmp_path):
    claude_guard.install(home)
    (home / "events").mkdir(exist_ok=True)
    payload = json.dumps({"tool_name": "Bash", "cwd": str(tmp_path),
                          "tool_input": {"command": f"rm -r {home}/events"}})
    r = subprocess.run([sys.executable, str(claude_guard.hook_script_path(home))],
                       input=payload, text=True, capture_output=True,
                       env={"MAESTRO_HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 2


def test_refuses_without_guard(home, tmp_path, launch, monkeypatch):
    empty = tmp_path / "no-assets"
    empty.mkdir()
    monkeypatch.setattr(claude_guard, "_package_asset_dir", lambda: empty)
    with pytest.raises(claude_guard.ClaudeGuardError):
        ClaudeCliSessions(home).spawn("X-1", "/maestro-reconcile", tmp_path)
    assert launch.calls == []


def test_refuses_when_settings_do_not_wire_the_hook(home, tmp_path, launch, monkeypatch):
    monkeypatch.setattr(claude_guard, "install", lambda h: claude_guard.settings_path(h))
    store.atomic_write(claude_guard.settings_path(home), "{}\n")
    with pytest.raises(claude_guard.ClaudeGuardError):
        ClaudeCliSessions(home).spawn("X-1", "/maestro-reconcile", tmp_path)
    assert launch.calls == []


def _seed_ready(home, key):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value, "reason": ""}, actor="d")
    snap_mod.rebuild(home, key)


def test_interlock_checks_spawning_runner(home, tmp_path, monkeypatch):
    # implementer=pi passes the provider interlock, yet the phase spawns under
    # claude (default runner): its missing guard must still refuse the spawn.
    cfg = Config(home=home, providers={"tracker": "none", "vcs": "none",
                                       "fetcher": "none", "implementer": "pi"})
    assert gates.backend_interlock_reason(cfg) is None
    empty = tmp_path / "no-assets"
    empty.mkdir()
    monkeypatch.setattr(claude_guard, "_package_asset_dir", lambda: empty)
    _seed_ready(home, "X-1")
    sess = DryRunSessions()
    report = disp.dispatch(cfg, sess, now=1000)
    assert "X-1" not in report.spawned and sess.spawned == []
    snap = snap_mod.load(home, "X-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert "claude destructive-command guard" in next(iter(snap.open_questions.values()))


def test_interlock_passes_for_a_guarded_claude_runner(home):
    cfg = Config(home=home)
    assert gates.runner_interlock_reason(cfg, "claude") is None
    assert claude_guard.settings_path(home).is_file()
    assert gates.runner_interlock_reason(cfg, "pi") is None
