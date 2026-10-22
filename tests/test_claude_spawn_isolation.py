"""T-213: claude spawns can pass --strict-mcp-config (knob) and always disable auto-memory.

Drives the real `maestro dispatch` sweep over a temp home; only `subprocess.Popen`
(the external `claude -p` launch) is substituted.
"""
import os
from unittest.mock import MagicMock, patch

from conftest import seed_phase
from maestro import cli
from maestro.statemachine import Phase


def _sweep(home, strict_mcp):
    lines = ["[maestro]", 'repo_path = "/repo/default"', 'branch_prefix = "maestro/"',
             "min_spawn_interval = 0"]
    if strict_mcp is not None:
        lines.append(f"reconcile_strict_mcp = {'true' if strict_mcp else 'false'}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    seed_phase(home, "T-1", Phase.READY)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    calls = []

    def capture_popen(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env") or {}))
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        assert cli.main(["--home", str(home), "dispatch"]) == 0
    claude = [c for c in calls if c[0] and c[0][0] == "claude"]
    assert claude, f"no claude spawn captured: {calls}"
    return claude[0]


def test_strict_mcp_knob(home):
    cmd, _ = _sweep(home, True)
    assert "--strict-mcp-config" in cmd


def test_strict_mcp_off_by_default(home):
    cmd, _ = _sweep(home, None)
    assert "--strict-mcp-config" not in cmd


def test_auto_memory_disabled(home):
    _, env = _sweep(home, None)
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
