"""PI-8: the `PiCliSessions` backend itself -- argv shape, claim shape,
watchdog reachability, and the fail-loud guards (missing guard extension, no
runner_model).

Argv-shape and claim-content assertions mock only `subprocess.Popen` (like
`test_sessions.py` does for `ClaudeCliSessions`, `test_opencode_sessions.py`
for `OpencodeCliSessions`); the claim/list_active/watchdog-reaping tests spawn
a REAL (stub) `pi` process, proving the actual external boundary this backend
owns.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import claims, dispatcher as disp, pi_guard, sessions as sessions_mod, store
from maestro.sessions import PiCliSessions, session_name


def _make_sessions(home: Path, clock_val: float = 1_000_000.0, **kw):
    return PiCliSessions(home=home, clock=lambda: clock_val, **kw)


def _capture_cmd(home, key="T-1", cwd=None, runner_model="glm-5.2",
                  clock_val=1_000_000.0, **sess_kw):
    sess = _make_sessions(home, clock_val=clock_val, capture_session_logs=False, **sess_kw)
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


# --- AC1: exact argv shape ----------------------------------------------------

def test_argv_is_exact_list(home):
    worktree = home / "worktrees" / "T-9"
    cmd, _ = _capture_cmd(home, key="T-9", cwd=worktree, runner_model="glm-5.2")

    guard_extension = pi_guard.install(store.pi_agent_dir(home))
    payload_dir = _payload_dir()

    assert cmd == [
        "pi", "-p", "/maestro-reconcile-implementing T-9",
        "--model", "zai/glm-5.2",
        "--mode", "json",
        "--no-skills",
        "--prompt-template", str(payload_dir),
        "--no-prompt-templates",
        "--extension", str(guard_extension),
        "--no-extensions",
        "--tools", ",".join(pi_guard.PI_GUARD_TOOLS),
        "--no-approve",
    ]
    assert "--approve" not in cmd
    assert "-a" not in cmd


def _payload_dir():
    from maestro import skills_install
    return skills_install.payload_dir()


def test_prompt_is_flattened_command_and_key(home):
    cmd, _ = _capture_cmd(home, key="T-42")
    assert cmd[0:3] == ["pi", "-p", "/maestro-reconcile-implementing T-42"]


def test_start_new_session_true(home):
    _cmd, kwargs = _capture_cmd(home)
    assert kwargs["start_new_session"] is True


def test_dir_equals_the_worktree_cwd(home):
    worktree = home / "worktrees" / "T-9"
    _cmd, kwargs = _capture_cmd(home, cwd=worktree)
    assert kwargs["cwd"] == str(worktree)


# --- AC2: the `<provider>/` prefix literal occurs exactly once in the package,
# matching the `ollama/` precedent (`OpencodeCliSessions.spawn`'s own
# `f"{self.model_prefix}/{runner_model}"`, composed only in that one method) --

def test_provider_slash_composition_occurs_exactly_once_in_the_package():
    pkg_dir = Path(sessions_mod.__file__).parent
    pattern = re.compile(r'f"\{provider\}/\{runner_model\}"')
    hits = []
    for path in pkg_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits += [(path, m.start()) for m in pattern.finditer(text)]
    assert len(hits) == 1, \
        f"expected the <provider>/<tag> composition exactly once in the package, found {hits}"
    assert hits[0][0] == Path(sessions_mod.__file__)


# --- AC3: --model always pairs provider+model; --provider alone never appears --

def test_provider_flag_alone_never_appears(home):
    cmd, _ = _capture_cmd(home, runner_model="glm-5.2")
    assert "--provider" not in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "zai/glm-5.2"


def test_model_composes_configured_provider_with_the_bare_tag(home, cfg):
    (home / "config.toml").write_text(
        "[maestro]\nrepo_path = \"/repo/default\"\nbranch_prefix = \"maestro/\"\n"
        "min_spawn_interval = 0\n\n[runner.pi]\nprovider = \"customvendor\"\n",
        encoding="utf-8")
    cmd, _ = _capture_cmd(home, runner_model="glm-5.2")
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "customvendor/glm-5.2"


# --- fail-loud when no runner_model resolved (should never happen -- the
# preflight is what's supposed to guarantee this) ----------------------------

def test_spawn_raises_without_runner_model(home):
    sess = _make_sessions(home, capture_session_logs=False)
    with patch("subprocess.Popen") as mock_popen:
        with pytest.raises(store.MaestroError):
            sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home)
    mock_popen.assert_not_called()


# --- AC5: fail-loud when the guard extension is missing ---------------------

def test_spawn_raises_when_guard_extension_missing(home, monkeypatch):
    sess = _make_sessions(home, capture_session_logs=False)
    missing_path = store.pi_agent_dir(home) / "extensions" / "does-not-exist.ts"
    monkeypatch.setattr(pi_guard, "install", lambda pi_agent_dir: missing_path)
    with patch("subprocess.Popen") as mock_popen:
        with pytest.raises(store.MaestroError, match="guard extension missing"):
            sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                       runner_model="glm-5.2")
    mock_popen.assert_not_called()
    assert claims.read_claim(home, "T-1") is None


# --- log capture: T-58's own pi log-identity slot -----------------------------

def test_log_file_uses_the_pi_log_identity_slot(home):
    sess = _make_sessions(home, clock_val=1_700_000_000.0, capture_session_logs=True)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=fake_proc):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                  runner_model="glm-5.2")
    expected = store.session_pi_path(
        home, "T-1", f"{session_name('T-1')}-1700000000.000000")
    assert expected.exists()
    c = claims.read_claim(home, "T-1")
    assert c["log_path"] == str(expected)


def test_unused_claude_only_kwargs_are_accepted_and_ignored(home):
    """model/effort/disallowed_tools/allowed_tools are accepted for call-site
    uniformity through RoutingSessions but have no pi equivalent."""
    sess = _make_sessions(home, capture_session_logs=False)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    with patch("subprocess.Popen", return_value=fake_proc):
        sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                  model="sonnet", effort="high",
                  disallowed_tools=["Bash(gh pr merge:*)"], allowed_tools=["Read"],
                  runner_model="glm-5.2")
    c = claims.read_claim(home, "T-1")
    assert c is not None


# --- T-52: claim reserved before Popen, rolled back on a failed launch ------

def test_claim_reserved_before_popen_launches(home):
    sess = _make_sessions(home, capture_session_logs=False)
    seen = {}

    def capture_popen(*args, **kwargs):
        seen["reservation"] = claims.read_claim(home, "T-1")
        fake_proc = MagicMock()
        fake_proc.pid = 999999
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        pid = sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                         runner_model="glm-5.2")

    reservation = seen["reservation"]
    assert reservation is not None, "no claim existed yet when Popen was called"
    assert reservation["pid"] == os.getpid()
    assert pid == 999999
    assert claims.read_claim(home, "T-1")["pid"] == 999999


def test_claim_rolled_back_when_popen_raises(home):
    sess = _make_sessions(home, capture_session_logs=False)
    with patch("subprocess.Popen", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                       runner_model="glm-5.2")
    assert claims.read_claim(home, "T-1") is None


# --- AC4: claim shape, list_active, real spawn + watchdog reap --------------

_STUB_PI = "#!/bin/sh\nexec sleep 30\n"


def _install_stub_pi(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    stub = bin_dir / "pi"
    stub.write_text(_STUB_PI)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return stub


def test_real_spawn_claim_has_the_same_field_shape_as_a_claude_spawn(home, tmp_path, monkeypatch):
    _install_stub_pi(tmp_path, monkeypatch)
    sess = PiCliSessions(home=home, capture_session_logs=True)
    pid = None
    try:
        pid = sess.spawn("T-1", "/maestro-reconcile-implementing", cwd=home,
                         runner_model="glm-5.2")
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


def test_run_watchdog_reaps_a_real_pi_spawn_by_pgid(home, cfg, tmp_path, monkeypatch):
    """AC4: `run_watchdog` kills by pgid -- proven by the stub CHILD process
    actually dying, not just the claim being released."""
    _install_stub_pi(tmp_path, monkeypatch)
    sess = PiCliSessions(home=home, capture_session_logs=False)
    from maestro import event_log, snapshot as snap_mod
    from maestro.statemachine import Phase

    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\ndependsOn: []\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    cfg.max_session_seconds = 100
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
