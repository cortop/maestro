"""pi log streaming: byte-prefix skip, auto-retry outcome, readable `maestro logs` (T-223)."""
import json
from pathlib import Path

import pytest

from maestro import steplog
from maestro.cli import main


def _write(path: Path, recs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # compact separators: the noise prefixes are matched on raw bytes
    path.write_bytes(b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in recs))
    return path


def _msg_end(stop="stop", err=None, text="hello there"):
    m = {"role": "assistant", "stopReason": stop, "provider": "acme",
         "model": "acme-1", "content": [{"type": "text", "text": text}]}
    if err:
        m["errorMessage"] = err
    return {"type": "message_end", "message": m}


NOISE = [
    {"type": "message_update", "delta": "tok"},
    {"type": "tool_execution_update", "toolCallId": "c1", "partialResult": "SECRET-PARTIAL"},
]


def _log(tmp_path, recs):
    return _write(tmp_path / "reconcile-T-1-100.000000.pi.jsonl", recs)


def test_iter_records_skip_prefixes_offsets_match(tmp_path):
    recs = [{"type": "session", "id": "s"}, NOISE[0], {"type": "turn_start"}, NOISE[1],
            _msg_end()]
    p = _log(tmp_path, recs)
    full = list(steplog.iter_records(p))
    skipped = list(steplog.iter_records(p, skip_prefixes=steplog.PI_NOISE_PREFIXES))
    assert [o["type"] for _, o in skipped] == ["session", "turn_start", "message_end"]
    full_offsets = {o["type"]: off for off, o in full}
    assert [off for off, _ in skipped] == [full_offsets[o["type"]] for _, o in skipped]
    assert skipped[-1][0] == p.stat().st_size


def test_skipped_lines_are_never_decoded(tmp_path, monkeypatch):
    p = _log(tmp_path, [NOISE[0], {"type": "turn_start"}, NOISE[1]])
    seen = []
    real = json.loads
    monkeypatch.setattr(steplog.json, "loads", lambda s, *a, **k: (seen.append(s), real(s))[1])
    list(steplog.iter_records(p, skip_prefixes=steplog.PI_NOISE_PREFIXES))
    assert len(seen) == 1 and "turn_start" in seen[0]


def test_readers_use_noise_skip(tmp_path, monkeypatch):
    p = _log(tmp_path, [{"type": "turn_start"}, NOISE[0], _msg_end(), {"type": "agent_end"}])
    calls = []
    real = steplog.iter_records

    def spy(path, **kw):
        calls.append(kw.get("skip_prefixes"))
        return real(path, **kw)

    monkeypatch.setattr(steplog, "iter_records", spy)
    assert steplog.session_outcome(p)["outcome"] == "success"
    assert steplog.session_model(p)["model"] == "acme-1"
    assert steplog._pi_session_outcome(p)["outcome"] == "success"
    assert calls and all(c == steplog.PI_NOISE_PREFIXES for c in calls)


def test_auto_retry_reports_retry_outcome(tmp_path):
    p = _log(tmp_path, [
        {"type": "turn_start"}, _msg_end("error", "overloaded"), {"type": "agent_end"},
        {"type": "auto_retry_start"},
        {"type": "turn_start"}, _msg_end("stop"), {"type": "agent_end"}, {"type": "agent_settled"},
    ])
    assert steplog.session_outcome(p)["outcome"] == "success"


def test_auto_retry_in_progress_is_running(tmp_path):
    p = _log(tmp_path, [{"type": "turn_start"}, _msg_end("error", "x"),
                        {"type": "agent_end"}, {"type": "auto_retry_start"}])
    assert steplog.session_outcome(p)["outcome"] == "running"


def test_auto_retry_exhausted_is_error(tmp_path):
    p = _log(tmp_path, [{"type": "turn_start"}, _msg_end("error", "x"), {"type": "agent_end"},
                        {"type": "auto_retry_start"}, _msg_end("error", "y"),
                        {"type": "agent_end"}, {"type": "agent_settled"}])
    out = steplog.session_outcome(p)
    assert out["outcome"] == "error" and out["result"]["result"] == "y"


PI_RECS = [
    {"type": "session", "id": "s"}, {"type": "turn_start"},
    {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
     "args": {"command": "ls -la"}},
    *NOISE,
    _msg_end(text="all done"), _msg_end("error", "boom", text=""), {"type": "agent_end"},
]


def test_logs_history_readable(home, capsys):
    _write(home / "agent-logs" / "T-1" / "reconcile-T-1-100.000000.pi.jsonl", PI_RECS)
    assert main(["--home", str(home), "logs", "T-1"]) == 0
    out = capsys.readouterr().out
    assert "[tool:bash] ls -la" in out
    assert "all done" in out and "[error] boom" in out
    assert "tool_execution_update" not in out and "SECRET-PARTIAL" not in out
    assert '"type"' not in out.split("===", 2)[-1]


def test_logs_follow_readable(home, capsys):
    _write(home / "agent-logs" / "T-1" / "reconcile-T-1-100.000000.pi.jsonl", PI_RECS)
    assert main(["--home", str(home), "logs", "T-1", "--follow"]) == 0
    out = capsys.readouterr().out
    assert "[tool:bash] ls -la" in out and "all done" in out
    assert "tool_execution_update" not in out and "SECRET-PARTIAL" not in out


def test_logs_json_is_byte_for_byte(home, capfdbinary):
    p = _write(home / "agent-logs" / "T-1" / "reconcile-T-1-100.000000.pi.jsonl", PI_RECS)
    assert main(["--home", str(home), "logs", "T-1", "--session",
                 "reconcile-T-1-100.000000", "--json"]) == 0
    assert capfdbinary.readouterr().out == p.read_bytes()


def test_copy_log_reads_in_bounded_chunks(tmp_path, monkeypatch):
    from maestro import cli
    big = tmp_path / "x.pi.jsonl"
    big.write_bytes(b'{"a":1}\n' * (3 << 17))  # ~3 MiB
    sizes = []
    real_open = Path.open

    class Spy:
        def __init__(self, fh): self.fh = fh
        def __enter__(self): self.fh.__enter__(); return self
        def __exit__(self, *a): return self.fh.__exit__(*a)
        def read(self, n=-1):
            sizes.append(n)
            return self.fh.read(n)

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
    cli._copy_log(big)
    assert sizes and all(0 < n <= (1 << 20) for n in sizes)
