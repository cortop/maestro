"""Tests for maestro logs <KEY> command (L-3)."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import store
from maestro.sessions import list_sessions, session_name

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_list_sessions_third_format_slot_and_baseline_unchanged(home):
    """RF-3: a third ('opencode') suffix reports its own format, and the existing
    .log / .stream.jsonl dicts stay byte-identical to the pre-change baseline shape."""
    log_path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    stream_path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    oc_path = home / "agent-logs" / "T-1" / "reconcile-T-1-3000.000000.opencode.jsonl"
    _make_text_log(log_path, "x")
    _make_text_log(stream_path, "{}")
    _make_text_log(oc_path, "{}")

    sessions = list_sessions(home, "T-1")
    by_fmt = {s["format"]: s for s in sessions}
    assert set(by_fmt) == {"text", "stream-json", "opencode"}
    assert by_fmt["text"] == {
        "session_id": "reconcile-T-1-1000.000000", "path": str(log_path),
        "format": "text", "epoch": 1000.0, "ts": by_fmt["text"]["ts"],
    }
    assert by_fmt["stream-json"] == {
        "session_id": "reconcile-T-1-2000.000000", "path": str(stream_path),
        "format": "stream-json", "epoch": 2000.0, "ts": by_fmt["stream-json"]["ts"],
    }
    assert by_fmt["opencode"] == {
        "session_id": "reconcile-T-1-3000.000000", "path": str(oc_path),
        "format": "opencode", "epoch": 3000.0, "ts": by_fmt["opencode"]["ts"],
    }


def test_list_sessions_fourth_format_slot_all_four_present(home):
    """AC1 (T-58): a home with all four session-log formats reports all four,
    each with the correct `format`, and pi's is never the literal "stream-json"."""
    log_path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    stream_path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    oc_path = home / "agent-logs" / "T-1" / "reconcile-T-1-3000.000000.opencode.jsonl"
    pi_path = home / "agent-logs" / "T-1" / "reconcile-T-1-4000.000000.pi.jsonl"
    _make_text_log(log_path, "x")
    _make_text_log(stream_path, "{}")
    _make_text_log(oc_path, "{}")
    _make_text_log(pi_path, "{}")

    sessions = list_sessions(home, "T-1")
    by_fmt = {s["format"]: s for s in sessions}
    assert set(by_fmt) == {"text", "stream-json", "opencode", "pi"}
    assert by_fmt["pi"] != "stream-json"
    assert by_fmt["pi"] == {
        "session_id": "reconcile-T-1-4000.000000", "path": str(pi_path),
        "format": "pi", "epoch": 4000.0, "ts": by_fmt["pi"]["ts"],
    }
    assert store.session_pi_path(home, "T-1", "reconcile-T-1-4000.000000") == pi_path


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


def test_cli_logs_list_third_format_without_raising(home, capsys):
    """AC3: a third-format log neither raises nor is misreported as anything
    other than 'running' while it carries no terminal step_finish record --
    OC-5 made '.opencode.jsonl' genuinely parseable, so a record outside the
    verified vocabulary (no step_finish seen) reports 'running', not the old
    blanket 'unknown'."""
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-9000.000000.opencode.jsonl"
    _make_text_log(path, '{"type": "message", "text": "hi"}\n')

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["format"] == "opencode"
    assert out[0]["outcome"] == "running"


def test_cli_logs_list_opencode_success_and_error_outcome(home, capsys):
    """AC3: real `maestro logs --list` reports success/error, derived from
    step_finish.part.reason, for a completed opencode session."""
    ok_path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.opencode.jsonl"
    _make_text_log(ok_path, "\n".join(json.dumps(r) for r in [
        {"type": "step_start", "part": {}},
        {"type": "tool_use", "part": {"tool": "bash", "callID": "call_1"}},
        {"type": "step_finish", "part": {"reason": "stop", "cost": 0}},
    ]) + "\n")

    err_path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.opencode.jsonl"
    _make_text_log(err_path, "\n".join(json.dumps(r) for r in [
        {"type": "step_start", "part": {}},
        {"type": "step_finish", "part": {"reason": "error", "cost": 0}},
    ]) + "\n")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    by_epoch = {s["epoch"]: s["outcome"] for s in out}
    assert by_epoch[1000.0] == "success"
    assert by_epoch[2000.0] == "error"


def test_cli_logs_opencode_renders_tool_use_and_text(home, capsys):
    """AC4: `maestro logs` renders opencode's own tool_use/text vocabulary
    (OC-5), not just a raw byte dump."""
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-9000.000000.opencode.jsonl"
    records = [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": "Reading the ticket spec."}},
        {"type": "tool_use", "part": {"tool": "bash", "callID": "call_1",
                                       "state": {"input": {"command": "pytest"}}}},
        {"type": "step_finish", "part": {"reason": "stop", "cost": 0}},
    ]
    _make_text_log(path, "\n".join(json.dumps(r) for r in records) + "\n")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Reading the ticket spec." in out
    assert "tool_use:bash" in out


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


def test_cli_logs_third_format_prints_raw_file(home, capsys):
    """AC4: a third-format log prints its raw content -- not an empty stream-json
    render (which would show nothing for a grammar it doesn't recognize)."""
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.opencode.jsonl"
    _make_text_log(path, '{"type": "message", "text": "hello from opencode"}\n')

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    assert "hello from opencode" in capsys.readouterr().out


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
# CLI: maestro logs — errored/rate-limited results and rate_limit_event lines
# ---------------------------------------------------------------------------

def test_cli_logs_rate_limited_result_not_rendered_as_success(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "rate_limited.stream.jsonl").read_bytes())

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[result:success]" not in out
    assert "429" in out
    assert "monthly spend limit" in out


def test_cli_logs_rate_limit_event_surfaced_not_dropped(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "rate_limited.stream.jsonl").read_bytes())

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "five_hour" in out
    assert "resetsAt=" in out


def test_cli_logs_follow_renders_rate_limited_not_success(home, capsys):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "rate_limited.stream.jsonl").read_bytes())

    from maestro import claims
    claims.write_claim(home, "T-1", pid=999999999, name="reconcile-T-1",
                       log_path=str(path))

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--follow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[result:success]" not in out
    assert "429" in out


# ---------------------------------------------------------------------------
# CLI: maestro logs --list carries a machine-readable `outcome` per session
# ---------------------------------------------------------------------------

def test_cli_logs_list_carries_outcome(home, capsys):
    success_path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.stream.jsonl"
    success_path.parent.mkdir(parents=True, exist_ok=True)
    success_path.write_bytes((FIXTURES / "sample.stream.jsonl").read_bytes())

    rl_path = home / "agent-logs" / "T-1" / "reconcile-T-1-2000.000000.stream.jsonl"
    rl_path.write_bytes((FIXTURES / "rate_limited.stream.jsonl").read_bytes())

    text_path = home / "agent-logs" / "T-1" / "reconcile-T-1-3000.000000.log"
    _make_text_log(text_path, "plain text")

    from maestro.cli import main
    rc = main(["--home", str(home), "logs", "T-1", "--list"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    by_epoch = {s["epoch"]: s["outcome"] for s in out}
    assert by_epoch[1000.0] == "success"
    assert by_epoch[2000.0] == "rate_limited"
    assert by_epoch[3000.0] == "unknown"


def test_list_sessions_default_has_no_outcome_and_opens_no_files(home, monkeypatch):
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "sample.stream.jsonl").read_bytes())

    real_open = Path.open

    def _guarded_open(self, *args, **kwargs):
        if self == path:
            raise AssertionError("list_sessions default call must not open log files")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _guarded_open)
    sessions = list_sessions(home, "T-1")
    assert "outcome" not in sessions[0]


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


def test_cli_logs_follow_stops_on_denied_pid_instead_of_blocking(home, capsys):
    """A claim pointing at a live pid that was REUSED (identity denied) must not
    make --follow poll pid_alive() forever — the reused process really is alive,
    so only the verified check can tell the loop to stop (T-17)."""
    path = home / "agent-logs" / "T-1" / "reconcile-T-1-1000.000000.log"
    _make_text_log(path, "line one\n")

    from maestro import claims

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        old_epoch = store.now_epoch() - 3600  # predates the child -> denied
        store.write_json(claims.claim_path(home, "T-1"),
                         {"pid": proc.pid, "name": "reconcile-T-1",
                          "ts": store.iso_now(), "epoch": old_epoch,
                          "log_path": str(path)})

        from maestro.cli import main
        result = {}

        def run():
            result["rc"] = main(["--home", str(home), "logs", "T-1", "--follow"])

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "logs --follow blocked on a denied (reused) pid"
        assert result["rc"] == 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)
