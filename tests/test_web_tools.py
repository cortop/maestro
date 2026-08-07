"""L-7: spawned reconciler sessions must get WebSearch/WebFetch via --allowedTools."""
import os
from unittest.mock import MagicMock, patch

from maestro import cli, event_log, inbox, snapshot as snap_mod, store
from maestro.config import load
from maestro.statemachine import Phase


def _seed_ready(home, key="T-1"):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "spec_hash": "x"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, key)


def _capture_popen_cmds():
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured = []

    def capture_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return fake_proc

    return captured, capture_popen


def test_config_reconcile_web_tools_defaults_true(home):
    cfg = load(str(home))
    assert cfg.reconcile_web_tools is True


def test_config_reconcile_web_tools_can_be_disabled(home):
    (home / "config.toml").write_text("[maestro]\nreconcile_web_tools = false\n")
    cfg = load(str(home))
    assert cfg.reconcile_web_tools is False


def test_dispatch_spawn_command_includes_web_search_and_fetch(home):
    """Real `maestro dispatch` (not dry-run) must pass --allowedTools WebSearch,WebFetch."""
    _seed_ready(home)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Bash(maestro:*),WebSearch,WebFetch"


def test_dispatch_still_grants_the_maestro_cli_when_web_tools_disabled(home):
    """Turning off web tools must not disarm the reconciler's own bookkeeping.

    Every phase skill records its step through the maestro CLI, so without this
    grant a reconciler spawned into a repo with no checked-in allow list stalls
    on a permission prompt no one is there to answer.
    """
    (home / "config.toml").write_text("[maestro]\nreconcile_web_tools = false\n")
    _seed_ready(home)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Bash(maestro:*)"


def test_nudge_spawn_command_includes_web_search_and_fetch(home):
    """The in-process nudge path (triggered by `ans`) must also grant the tools."""
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1", "spec_hash": "x"}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})

    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Bash(maestro:*),WebSearch,WebFetch"
