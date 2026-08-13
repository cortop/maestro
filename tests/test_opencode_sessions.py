"""OC-4: the `OpencodeCliSessions` backend itself -- argv shape, claim shape,
watchdog reachability, and the "no runner_model, no spawn" fail-loud guard.

Argv-shape and claim-content assertions mock only `subprocess.Popen` (like
`test_sessions.py` does for `ClaudeCliSessions`); the claim/list_active/
watchdog-reaping tests spawn a REAL (stub) `opencode` process, proving the
actual external boundary this backend owns -- exactly the same posture
`test_dispatcher.py`'s watchdog tests take with a real `sleep` process.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from maestro import claims, dispatcher as disp, store
from maestro.sessions import OpencodeCliSessions, session_name


def _make_sessions(home: Path, clock_val: float = 1_000_000.0, **kw):
    return OpencodeCliSessions(home=home, clock=lambda: clock_val, **kw)


def _capture_cmd(home, key="T-1", cwd=None, runner_model="qwen3-coder:30b",
                  clock_val=1_000_000.0):
    sess = _make_sessions(home, clock_val=clock_val, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_cmd = []
    captured_kwargs = {}

    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn(key, "/maestro-reconcile-implementing", cwd=cwd or home,
                  runner_model=runner_model)
    return captured_cmd, captured_kwargs


# --- AC2: exact argv shape ----------------------------------------------------

def test_argv_is_exact_list(home):
    worktree = home / "worktrees" / "T-9"
    cmd, _ = _capture_cmd(home, key="T-9", cwd=worktree, runner_model="qwen3-coder:30b")
    assert cmd == [
        "opencode", "run",
        "--command", "maestro-reconcile-implementing",
        "--model", "ollama/qwen3-coder:30b",
        "--format", "json",
        "--dir", str(worktree),
        "--agent", "maestro-reconcile-implementing",
    ]


def test_argv_never_carries_auto_flag(home):
    cmd, _ = _capture_cmd(home)
    assert "--auto" not in cmd


def test_dir_equals_the_worktree_cwd(home):
    worktree = home / "worktrees" / "T-9"
    cmd, kwargs = _capture_cmd(home, cwd=worktree)
    idx = cmd.index("--dir")
    assert cmd[idx + 1] == str(worktree)
    assert kwargs["cwd"] == str(worktree)


def test_command_and_agent_share_the_same_bare_name_stripped_of_leading_slash(home):
    cmd, _ = _capture_cmd(home)
    command_idx = cmd.index("--command")
    agent_idx = cmd.index("--agent")
    assert cmd[command_idx + 1] == cmd[agent_idx + 1] == "maestro-reconcile-implementing"


def test_model_composes_ollama_prefix_with_the_bare_tag(home):
    cmd, _ = _capture_cmd(home, runner_model="qwen3-coder:30b")
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "ollama/qwen3-coder:30b"


def test_start_new_session_true(home):
    _cmd, kwargs = _capture_cmd(home)
    assert kwargs["start_new_session"] is True


# --- fail-loud when no runner_model resolved (should never happen -- OC-2's
# own preflight is what's supposed to guarantee this) ------------------------

def test_spawn_raises_without_runner_model(home):
    sess = _make_sessions(home, capture_session_logs=False)
    with patch("subprocess.Popen") as mock_popen:
        try:
            sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home)
            assert False, "expected a MaestroError"
        except store.MaestroError:
            pass
    mock_popen.assert_not_called()


# --- log capture: RF-3's own opencode log-identity slot ----------------------

def test_log_file_uses_the_opencode_log_identity_slot(home):
    sess = _make_sessions(home, clock_val=1_700_000_000.0, capture_session_logs=True)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=fake_proc):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                  runner_model="a:1b")
    expected = store.session_opencode_path(
        home, "T-1", f"{session_name('T-1')}-1700000000.000000")
    assert expected.exists()
    c = claims.read_claim(home, "T-1")
    assert c["log_path"] == str(expected)


def test_unused_claude_only_kwargs_are_accepted_and_ignored(home):
    """model/effort/disallowed_tools/allowed_tools are accepted for call-site
    uniformity through RoutingSessions but have no opencode equivalent."""
    sess = _make_sessions(home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=fake_proc):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                  model="sonnet", effort="high",
                  disallowed_tools=["Bash(gh pr merge:*)"], allowed_tools=["Read"],
                  runner_model="a:1b")
    # no exception -- ignored, never surfaced in argv
    c = claims.read_claim(home, "T-1")
    assert c is not None


# --- claim shape: same field shape a Claude spawn writes (AC3) --------------

_STUB_OPENCODE = "#!/bin/sh\nexec sleep 30\n"


def _install_stub_opencode(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(_STUB_OPENCODE)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return stub


def test_real_spawn_claim_has_the_same_field_shape_as_a_claude_spawn(home, tmp_path, monkeypatch):
    _install_stub_opencode(tmp_path, monkeypatch)
    sess = OpencodeCliSessions(home=home, capture_session_logs=True)
    pid = None
    try:
        pid = sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                         runner_model="qwen3-coder:30b")
        assert pid is not None

        c = claims.read_claim(home, "T-1")
        assert c is not None
        for field in ("pid", "name", "ts", "epoch", "log_path", "cwd", "prompt"):
            assert field in c, f"missing {field!r} -- claim shape must match ClaudeCliSessions"
        assert c["pid"] == pid
        assert c["name"] == session_name("T-1")
        assert c["cwd"] == str(home)

        assert "T-1" in claims.active_keys(home)

        # start_new_session=True -- pid IS the process group leader.
        assert os.getpgid(pid) == pid
    finally:
        if pid is not None:
            try:
                os.killpg(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_run_watchdog_reaps_a_real_opencode_spawn_by_pgid(home, cfg, tmp_path, monkeypatch):
    """AC3: `run_watchdog` kills by pgid (dispatcher.py's `pid == pgid`
    requirement) -- proven by the stub CHILD process actually dying, not just
    the claim being released."""
    _install_stub_opencode(tmp_path, monkeypatch)
    sess = OpencodeCliSessions(home=home, capture_session_logs=False)
    from maestro import event_log, snapshot as snap_mod
    from maestro.statemachine import Phase

    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\ndependsOn: []\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    cfg.max_session_seconds = 100
    pid = sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home, runner_model="a:1b")
    try:
        # age the claim past the threshold, same helper shape as test_dispatcher.py
        c = claims.read_claim(home, "T-1")
        c["epoch"] = store.now_epoch() - 10_000
        store.write_json(claims.claim_path(home, "T-1"), c)

        reaped = disp.run_watchdog(cfg, now=store.now_epoch())

        assert reaped == ["T-1"]
        assert claims.read_claim(home, "T-1") is None

        # This test process is the real OS parent of the stub (spawned it via
        # sess.spawn() -> subprocess.Popen above) -- os.waitpid, not a naive
        # `os.kill(pid, 0)` liveness poll, is the only race-free way to observe
        # its death: exactly the pid-reuse hazard claims.py's own verified-identity
        # check exists to avoid (a bare `kill(pid, 0)` could "see" an unrelated
        # process that reused this pid moments after the real child was reaped).
        reaped_pid = 0
        for _ in range(30):
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            if reaped_pid == pid:
                break
            time.sleep(0.1)
        assert reaped_pid == pid, "stub opencode process was not reaped by the watchdog's SIGTERM"
    finally:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
