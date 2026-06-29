"""Tests for session log capture (L-1)."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import claims, store
from maestro.sessions import ClaudeCliSessions, session_name


def _make_sessions(home: Path, capture: bool = True, clock_val: float = 1_000_000.0):
    return ClaudeCliSessions(
        home=home,
        capture_session_logs=capture,
        session_log_format="text",  # L-1 text-capture behavior
        clock=lambda: clock_val,
    )


def _fake_popen(home: Path, key: str, capture: bool = True, clock_val: float = 1_000_000.0):
    """Spawn with a mock Popen that returns a live pid."""
    sess = _make_sessions(home, capture=capture, clock_val=clock_val)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        pid = sess.spawn(key, "test-prompt", cwd=home)
    return pid, mock_popen


def test_log_file_created_when_capture_enabled(home):
    pid, _ = _fake_popen(home, "T-1", capture=True, clock_val=1_700_000_000.0)
    expected = store.session_log_path(home, "T-1", f"{session_name('T-1')}-1700000000.000000")
    assert expected.exists()


def test_log_file_not_created_when_capture_disabled(home):
    _fake_popen(home, "T-1", capture=False)
    log_dir = home / "agent-logs" / "T-1"
    assert not log_dir.exists()


def test_log_path_stored_in_claim_when_capture_enabled(home):
    _fake_popen(home, "T-1", capture=True, clock_val=1_700_000_000.0)
    c = claims.read_claim(home, "T-1")
    assert c is not None
    assert "log_path" in c
    assert "T-1" in c["log_path"]
    assert c["log_path"].endswith(".log")


def test_no_log_path_in_claim_when_capture_disabled(home):
    _fake_popen(home, "T-1", capture=False)
    c = claims.read_claim(home, "T-1")
    assert c is not None
    assert "log_path" not in c


def test_distinct_keys_write_distinct_log_files(home):
    _fake_popen(home, "T-1", capture=True, clock_val=1_000.0)
    _fake_popen(home, "T-2", capture=True, clock_val=2_000.0)
    log_t1 = store.session_log_path(home, "T-1", f"{session_name('T-1')}-1000.000000")
    log_t2 = store.session_log_path(home, "T-2", f"{session_name('T-2')}-2000.000000")
    assert log_t1.exists()
    assert log_t2.exists()
    assert log_t1 != log_t2


def test_popen_receives_log_file_for_stdout_stderr_when_capture(home):
    sess = _make_sessions(home, capture=True, clock_val=5_000.0)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_kwargs = {}
    def capture_popen(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home)
    assert captured_kwargs.get("stdout") is not subprocess.DEVNULL
    assert captured_kwargs.get("stderr") is not subprocess.DEVNULL
    assert captured_kwargs.get("stdout") is captured_kwargs.get("stderr")


def test_popen_receives_devnull_when_capture_disabled(home):
    sess = _make_sessions(home, capture=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_kwargs = {}
    def capture_popen(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home)
    assert captured_kwargs.get("stdout") is subprocess.DEVNULL
    assert captured_kwargs.get("stderr") is subprocess.DEVNULL


# --- RT-1: per-ticket model & effort overrides ---

def _capture_cmd(home, key="T-1", model=None, effort=None, clock_val=1_000_000.0):
    """Spawn and capture the exact command list passed to Popen."""
    sess = ClaudeCliSessions(
        home=home, capture_session_logs=False, clock=lambda: clock_val
    )
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_cmd = []
    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn(key, "prompt", cwd=home, model=model, effort=effort)
    return captured_cmd


def test_spawn_uses_instance_model_by_default(home):
    sess = ClaudeCliSessions(home=home, model="haiku", capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_cmd = []
    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return fake_proc
    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn("T-1", "p", cwd=home)
    idx = captured_cmd.index("--model")
    assert captured_cmd[idx + 1] == "haiku"


def test_spawn_model_override_replaces_instance_model(home):
    cmd = _capture_cmd(home, model="opus")
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "opus"


def test_spawn_no_effort_flag_when_effort_is_none(home):
    cmd = _capture_cmd(home, effort=None)
    assert "--effort" not in cmd


def test_spawn_effort_flag_appended_before_permission_mode(home):
    cmd = _capture_cmd(home, effort="high")
    assert "--effort" in cmd
    effort_pos = cmd.index("--effort")
    assert cmd[effort_pos + 1] == "high"
    # effort must come before --permission-mode (which follows model+effort)
    assert "--permission-mode" not in cmd or effort_pos < cmd.index("--permission-mode")


def test_dryrun_records_model_and_effort():
    from maestro.sessions import DryRunSessions
    from pathlib import Path
    s = DryRunSessions()
    s.spawn("T-1", "prompt", Path("/tmp"), model="opus", effort="high")
    key, prompt, cwd, model, effort = s.spawned[0]
    assert key == "T-1"
    assert model == "opus"
    assert effort == "high"


def test_dryrun_records_none_when_not_provided():
    from maestro.sessions import DryRunSessions
    from pathlib import Path
    s = DryRunSessions()
    s.spawn("T-1", "prompt", Path("/tmp"))
    assert s.spawned[0][3] is None  # model
    assert s.spawned[0][4] is None  # effort
