"""Tests for structured stream-json log capture (L-2).

Schema fields that L-4 depends on:
  - type       : event kind — "system" | "assistant" | "user" | "result"
  - subtype    : qualifier — "init" on system; on result it is NOT the verdict (a 429
                 rejection reports subtype "success") — see steplog.classify_result,
                 which derives the outcome from is_error / api_error_status instead.
  - name       : tool name, in message.content[].name where content[].type == "tool_use"
  - id         : message id in message.id; tool call id in content[].id
"""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import claims, store
from maestro.sessions import ClaudeCliSessions, session_name

FIXTURE = Path(__file__).parent / "fixtures" / "sample.stream.jsonl"


def _make_sessions(home: Path, fmt: str = "stream-json", capture: bool = True,
                   clock_val: float = 1_000_000.0) -> ClaudeCliSessions:
    return ClaudeCliSessions(
        home=home,
        capture_session_logs=capture,
        session_log_format=fmt,
        clock=lambda: clock_val,
    )


def _fake_popen(home: Path, key: str, fmt: str = "stream-json", capture: bool = True,
                clock_val: float = 1_000_000.0):
    sess = _make_sessions(home, fmt=fmt, capture=capture, clock_val=clock_val)
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured_cmd: list = []
    captured_kwargs: dict = {}

    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        return fake_proc

    with patch("subprocess.Popen", side_effect=capture_popen):
        sess.spawn(key, "test-prompt", cwd=home)
    return captured_cmd, captured_kwargs


# --- spawn command flags ---

def test_stream_json_adds_output_format_flag(home):
    cmd, _ = _fake_popen(home, "T-1", fmt="stream-json")
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_stream_json_adds_verbose_flag(home):
    cmd, _ = _fake_popen(home, "T-1", fmt="stream-json")
    assert "--verbose" in cmd


def test_text_format_no_output_format_flag(home):
    cmd, _ = _fake_popen(home, "T-1", fmt="text")
    assert "--output-format" not in cmd
    assert "--verbose" not in cmd


# --- log file paths ---

def test_stream_json_creates_stream_jsonl_file(home):
    _fake_popen(home, "T-1", fmt="stream-json", clock_val=1_700_000_000.0)
    expected = store.session_stream_path(home, "T-1", f"{session_name('T-1')}-1700000000.000000")
    assert expected.exists()


def test_text_format_creates_log_file(home):
    _fake_popen(home, "T-1", fmt="text", clock_val=1_700_000_000.0)
    expected = store.session_log_path(home, "T-1", f"{session_name('T-1')}-1700000000.000000")
    assert expected.exists()


def test_stream_json_claim_path_ends_with_stream_jsonl(home):
    _fake_popen(home, "T-1", fmt="stream-json", clock_val=1_700_000_000.0)
    c = claims.read_claim(home, "T-1")
    assert c is not None and c["log_path"].endswith(".stream.jsonl")


def test_text_format_claim_path_ends_with_log(home):
    _fake_popen(home, "T-1", fmt="text", clock_val=1_700_000_000.0)
    c = claims.read_claim(home, "T-1")
    assert c is not None and c["log_path"].endswith(".log")


# --- fixture validity ---

def test_fixture_is_valid_jsonl():
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    assert len(lines) > 0
    for line in lines:
        json.loads(line)  # must not raise


def test_fixture_ends_with_result_object():
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    last = json.loads(lines[-1])
    assert last["type"] == "result"


def test_fixture_tool_use_count():
    """Fixture has exactly 2 tool_use steps (Read + Bash)."""
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    count = sum(
        1
        for line in lines
        for obj in [json.loads(line)]
        if obj.get("type") == "assistant"
        for block in obj.get("message", {}).get("content", [])
        if block.get("type") == "tool_use"
    )
    assert count == 2


def test_fixture_schema_fields():
    """Every object has 'type'; tool_use blocks have 'name' and 'id'."""
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    for line in lines:
        obj = json.loads(line)
        assert "type" in obj
        if obj["type"] == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    assert "name" in block, "tool_use block missing 'name'"
                    assert "id" in block, "tool_use block missing 'id'"


def test_fixture_system_init_subtype():
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    first = json.loads(lines[0])
    assert first["type"] == "system"
    assert first["subtype"] == "init"


def test_fixture_result_has_subtype():
    lines = [l for l in FIXTURE.read_text().splitlines() if l.strip()]
    last = json.loads(lines[-1])
    assert "subtype" in last
