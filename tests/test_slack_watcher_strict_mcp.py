"""The Slack watcher under `reconcile_strict_mcp`: its spawn loads only `slack_mcp_config`.

Drives the real `maestro dispatch` sweep and `maestro doctor` over a temp home; only
`subprocess.Popen` (the external `claude -p` launch) is substituted. Fixtures are synthetic.
"""
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

from conftest import run_doctor, seed_phase
from maestro import cli, slack_watcher
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

TOOL = "mcp__chat__read_channel"
MCP_CONFIG = '{"mcpServers": {"chat": {"type": "http", "url": "https://chat.example/mcp"}}}'


def _write_cfg(home, *extra):
    lines = ["[maestro]", 'repo_path = "/repo/default"', 'branch_prefix = "maestro/"',
             "min_spawn_interval = 0", "reconcile_strict_mcp = true",
             'slack_channel = "C0SYNTH"', 'slack_user_id = "U111AAA"',
             f'slack_read_tools = ["{TOOL}"]', 'slack_active_hours = ""', *extra]
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sweep(home):
    """One real `maestro dispatch`; return the argv of every claude launch (only those are faked)."""
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    real_popen = subprocess.Popen
    calls = []

    def capture_popen(cmd, *args, **kwargs):
        if not (isinstance(cmd, list) and cmd and cmd[0] == "claude"):
            return real_popen(cmd, *args, **kwargs)  # the claim probe's `ps` stays real
        calls.append(cmd)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        assert cli.main(["--home", str(home), "dispatch"]) == 0
    return calls


def _slack_check(home):
    _code, report = run_doctor(home)
    return next(c for c in report["checks"] if c["name"] == "slack_tools")


def test_strict_watcher_spawn_loads_only_its_mcp_config(home):
    _write_cfg(home, f"slack_mcp_config = '{MCP_CONFIG}'")
    seed_phase(home, "T-1", Phase.READY)
    spawns = {cmd[cmd.index("-n") + 1]: cmd for cmd in _sweep(home)}
    watcher = spawns.pop(f"reconcile-{slack_watcher.WATCHER_KEY}")
    assert "--strict-mcp-config" in watcher
    assert watcher[watcher.index("--mcp-config") + 1] == MCP_CONFIG
    assert spawns, "the ticket's reconciler should spawn too"
    assert all("--strict-mcp-config" in c and "--mcp-config" not in c for c in spawns.values())


def test_strict_without_mcp_config_never_spawns_the_watcher(home):
    cfg = Config(home=home, max_concurrency=3, reconcile_strict_mcp=True, slack_channel="C0SYNTH",
                 slack_user_id="U111AAA", slack_read_tools=[TOOL], slack_active_hours="")
    sessions = DryRunSessions()
    assert slack_watcher.sync(cfg, sessions, 1_000_000.0) == {
        "spawned": False, "reason": "strict MCP without slack_mcp_config"}
    assert sessions.spawned == []


def test_doctor_warns_strict_without_mcp_config(home):
    _write_cfg(home)
    check = _slack_check(home)
    assert check["status"] == "warn" and check["missing"] == [TOOL]
    assert "slack_mcp_config" in check["detail"]


def test_doctor_reads_servers_from_the_mcp_config_string_or_file(home):
    _write_cfg(home, f"slack_mcp_config = '{MCP_CONFIG}'")
    assert _slack_check(home)["status"] == "ok"
    (home / "slack-mcp.json").write_text(MCP_CONFIG, encoding="utf-8")
    _write_cfg(home, 'slack_mcp_config = "slack-mcp.json"')
    assert _slack_check(home)["status"] == "ok"


def test_doctor_warns_when_the_mcp_config_lacks_the_tool_server(home):
    other = json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://x.example"}}})
    _write_cfg(home, f"slack_mcp_config = '{other}'")
    check = _slack_check(home)
    assert check["status"] == "warn" and check["missing"] == [TOOL]
    _write_cfg(home, 'slack_mcp_config = "missing.json"')
    check = _slack_check(home)
    assert check["status"] == "warn" and "not a readable MCP config" in check["detail"]
