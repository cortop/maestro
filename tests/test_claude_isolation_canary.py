"""T-213: env-gated real-claude canary. Skipped unless MAESTRO_CLAUDE_CANARY=1.

Runs a real `claude -p` with the isolation settings and checks the stream-json
`system`/`init` record lists no MCP servers and no auto-memory path.
"""
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_CLAUDE_CANARY") != "1" or shutil.which("claude") is None,
    reason="set MAESTRO_CLAUDE_CANARY=1 (and have `claude` on PATH) to run the real-claude canary")


def test_isolated_session_init_has_no_mcp_or_memory(tmp_path):
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    proc = subprocess.run(
        ["claude", "-p", "reply with the single word ok", "--strict-mcp-config",
         "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    init = None
    for line in proc.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "init":
            init = rec
            break
    assert init is not None, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert init.get("mcp_servers") in ([], None), init.get("mcp_servers")
    assert "memory" not in json.dumps(init).lower().replace("memory_paths\": []", ""), \
        "init record references an auto-memory path"
