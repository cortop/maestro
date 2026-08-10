"""QW-5 (T-27): `_nudge` already passed `capture_session_logs=cfg.capture_session_logs`
through to `ClaudeCliSessions`; `cmd_dispatch` -- the launchd fleet's real spawn path --
silently inherited the `True` default instead. Every test here drives the real `maestro`
CLI (`cli.main([...])`) over a temp home; the only substituted boundary is the external
one (`cli.ClaudeCliSessions` itself, or the `subprocess.Popen` it shells out through --
never `dispatch()`/`ops`/`snapshot` themselves).
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from maestro import cli, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.statemachine import Phase


def _write_config(home, *, capture_session_logs=None):
    lines = ["[maestro]", 'repo_path = "/repo/default"', 'branch_prefix = "maestro/"',
              "min_spawn_interval = 0"]
    if capture_session_logs is not None:
        lines.append(f"capture_session_logs = {'true' if capture_session_logs else 'false'}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_ready(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, key)


# --- AC1: `maestro dispatch` constructs ClaudeCliSessions honouring the knob ------

def test_dispatch_constructs_session_manager_with_capture_session_logs_false(home):
    _write_config(home, capture_session_logs=False)
    captured = {}

    def spy(*args, **kwargs):
        captured.update(kwargs)
        from maestro.sessions import DryRunSessions
        return DryRunSessions()

    with patch("maestro.cli.ClaudeCliSessions", side_effect=spy):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert captured["capture_session_logs"] is False


def test_dispatch_constructs_session_manager_with_capture_session_logs_true_when_unset(home):
    # No capture_session_logs key in config.toml at all -- must inherit the True default.
    _write_config(home)
    captured = {}

    def spy(*args, **kwargs):
        captured.update(kwargs)
        from maestro.sessions import DryRunSessions
        return DryRunSessions()

    with patch("maestro.cli.ClaudeCliSessions", side_effect=spy):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert captured["capture_session_logs"] is True


# --- AC2: capture_session_logs=false really means no file under agent-logs/ ------

def test_dispatch_capture_session_logs_false_writes_no_agent_log_file(home):
    _write_config(home, capture_session_logs=False)
    _seed_ready(home, "T-1")

    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()

    def capture_popen(cmd, **kwargs):
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0

    agent_logs = home / "agent-logs"
    written = list(agent_logs.rglob("*")) if agent_logs.exists() else []
    assert written == [], f"expected no agent-logs files, found {written}"


# --- AC3: drift guard -- _nudge and cmd_dispatch build the manager from the same kwargs

def test_nudge_and_dispatch_construct_session_manager_with_same_kwargs(home, monkeypatch):
    """Guards future drift: if either call site gains/loses a constructor
    keyword without the other, this fails loudly instead of silently."""
    from maestro import inbox

    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "T-1", "spec_hash": disp.spec_hash_on_disk(home, "T-1")}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})

    nudge_kwargs = {}

    def nudge_spy(*args, **kwargs):
        nudge_kwargs.update(kwargs)
        from maestro.sessions import DryRunSessions
        return DryRunSessions()

    with patch("maestro.cli.ClaudeCliSessions", side_effect=nudge_spy):
        rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert nudge_kwargs  # the nudge really constructed a session manager

    dispatch_kwargs = {}

    def dispatch_spy(*args, **kwargs):
        dispatch_kwargs.update(kwargs)
        from maestro.sessions import DryRunSessions
        return DryRunSessions()

    with patch("maestro.cli.ClaudeCliSessions", side_effect=dispatch_spy):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert dispatch_kwargs

    assert set(nudge_kwargs.keys()) == set(dispatch_kwargs.keys())
    assert "capture_session_logs" in nudge_kwargs and "capture_session_logs" in dispatch_kwargs
