"""Tests for maestro logs <KEY> command (L-3)."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import store
from maestro.sessions import list_sessions, session_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_stream_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _make_stream_events(text_blocks=None, tool_uses=None) -> list[dict]:
    """Build a minimal stream-jsonl event list with one assistant message."""
    content = []
    for t in (text_blocks or []):
        content.append({"type": "text", "text": t})
    for name, inp in (tool_uses or []):
        content.append({"type": "tool_use", "name": name, "input": inp, "id": "t1"})
    return [
        {"type": "assistant", "message": {"id": "msg_001", "role": "assistant", "content": content}},
        {"type": "result", "subtype": "success", "duration_ms": 1234},
    ]


def _make_text_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------

def test_list_sessions_empty(home):
    assert list_sessions(home, "T-1") == []


def test_list_sessions_newest_first(home):
    for epoch in ("1000.000000", "3000.000000", "2000.000000"):
        sid = f"reconcile-T-1-{epoch}"
        path = home / "agent-logs" / "T-1" / f"{sid}.log"
        _make_text_log(path, "x")

    sessions = list_sessions(home, "T-1")
    assert len(sessions) == 3
    epochs = [s["epoch"] for s in sessions]
    assert epochs == sorted(epochs, reverse=True)


def test_list_sessions_format_detection(home):
    log_path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    jsonl_path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    _make_text_log(log_path, "x")
    _make_text_log(jsonl_path, "{}")

    sessions = list_sessions(home, "T-1")
    by_fmt = {s["format"]: s for s in sessions}
    assert "text" in by_fmt
    assert "stream-json" in by_fmt


def test_list_sessions_session_id(home):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-5000.000000.log"
    _make_text_log(path, "hello")
    sessions = list_sessions(home, "T-1")
    assert sessions[0]["session_id"] == "reconcile-T-1-5000.000000"


def test_list_sessions_ignores_unknown_files(home):
    (home / "agent-logs" / "T-1").mkdir(parents=True, exist_ok=True)
    (home / "agent-logs" / "T-1" / "README.txt").write_text("x")
    assert list_sessions(home, "T-1") == []


# ---------------------------------------------------------------------------
# CLI: maestro logs --list
# ---------------------------------------------------------------------------

def test_cli_logs_list(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-9000.000000.log"
    _make_text_log(path, "hi")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["session_id"] == "reconcile-T-1-9000.000000"


def test_cli_logs_list_no_sessions(home, capsys):
    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--list"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []


# ---------------------------------------------------------------------------
# CLI: maestro logs (default: newest session, human-readable)
# ---------------------------------------------------------------------------

def test_cli_logs_no_sessions(home, capsys):
    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 1
    assert "No session logs" in capsys.readouterr().err


def test_cli_logs_text_format(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    _make_text_log(path, "line one\nline two\n")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    assert "line one" in capsys.readouterr().out


def test_cli_logs_stream_renders_human_readable(home, capsys):
    events = _make_stream_events(text_blocks=["Hello from the agent."])
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    _write_stream_jsonl(path, events)

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Hello from the agent." in out
    assert "[result:success]" in out


def test_cli_logs_stream_renders_tool_use(home, capsys):
    events = _make_stream_events(tool_uses=[("Bash", {"command": "ls"})])
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    _write_stream_jsonl(path, events)

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    assert "tool_use:Bash" in capsys.readouterr().out


def test_cli_logs_json_emits_raw_lines(home, capsys):
    events = _make_stream_events(text_blocks=["hi"])
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    _write_stream_jsonl(path, events)

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--json"])
    assert rc == 0
    raw = capsys.readouterr().out
    # Each line should be valid JSON
    lines = [l for l in raw.strip().splitlines() if l]
    assert all(json.loads(l) for l in lines)


# ---------------------------------------------------------------------------
# CLI: maestro logs --session <id>
# ---------------------------------------------------------------------------

def test_cli_logs_session_select(home, capsys):
    for epoch, content in [("1000.000000", "old session"), ("2000.000000", "new session")]:
        path = home / "agent-logs" / "T-1" / f"reconcile-T-1-{epoch}.log"
        _make_text_log(path, content)

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--session", "reconcile-T-1-1000.000000"])
    assert rc == 0
    assert "old session" in capsys.readouterr().out


def test_cli_logs_session_not_found(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    _make_text_log(path, "x")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--session", "reconcile-T-1-9999.000000"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI: maestro logs --follow (exits when pid dies)
# ---------------------------------------------------------------------------

def test_cli_logs_follow_exits_when_pid_gone(home, capsys, tmp_path):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    _make_text_log(path, "line one\n")

    # Write a claim with a pid that is NOT alive
    from maestro import claims
    claims.write_claim(home, "T-1", pid=999999999, name="reconcile-T-1",
                       log_path=str(path))

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--follow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "line one" in out
