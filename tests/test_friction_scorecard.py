"""T-211: `maestro scorecard friction` -- tool-failure signatures per runner and phase."""
import json
import re
import subprocess
import sys

from maestro import cli, store

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def _ev(home, key, rows):
    lines = [json.dumps({"seq": i, "ts": ts, "key": key, "actor": "t", "type": "PhaseChanged",
                         "step_id": None, "fencing_token": i, "payload": {"phase": ph, "reason": ""}})
             for i, (ts, ph) in enumerate(rows, 1)]
    p = store.events_path(home, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


def _pi(home, key, epoch, results, ext="pi.jsonl"):
    d = home / "agent-logs" / key
    d.mkdir(parents=True, exist_ok=True)
    recs = [{"type": "session", "id": "s"}]
    for i, (text, is_err) in enumerate(results):
        recs.append({"type": "tool_execution_end", "toolCallId": f"c{i}", "toolName": "bash",
                     "result": {"content": [{"type": "text", "text": text}]}, "isError": is_err})
    (d / f"reconcile-{key}-{epoch}.{ext}").write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _board(home):
    _ev(home, "T-1", [("2026-09-01T10:00:00+00:00", "implementing"), ("2026-09-01T12:00:00+00:00", "qa")])
    # 2026-09-01T11:00Z -> implementing
    _pi(home, "T-1", 1788260400.0, [
        ("error: unknown option '-C' for gt\n", False),
        ("ok", False),
        (f"export TOKEN={SECRET}\nbash: foo: command not found", True),
    ])
    # before first phase -> excluded
    _pi(home, "T-1", 1700000000.0, [("ok", False)])
    # non-pi runner -> excluded
    _pi(home, "T-1", 1788260500.0, [], ext="stream.jsonl")


def _run(home, capsys):
    capsys.readouterr()
    assert cli.main(["--home", str(home), "scorecard", "friction"]) == 0
    return (home / "derived" / "FRICTION.md").read_text()


def test_scorecard_over_fixture_logs(home, capsys):
    _board(home)
    out = _run(home, capsys)
    assert re.search(r"\| pi \| implementing \| 1 \| 3 \| 2 \|", out)
    assert "gt-invalid-dash-C" in out and "command-not-found" in out
    assert "no phase at spawn: 1" in out and "runner not supported (pi only): 1" in out


def test_matches_masked_failures(home, capsys):
    _board(home)
    # the masked gt failure is reported with isError false (`| tail` hid the exit code)
    out = _run(home, capsys)
    assert "`gt-invalid-dash-C`: 1 in 1 sessions" in out


def test_output_redacted(home, capsys):
    _board(home)
    out = _run(home, capsys)
    assert SECRET not in out and "[redacted]" in out


def test_not_in_sweep():
    src = subprocess.run([sys.executable, "-c",
                          "import maestro.dispatcher, sys; print('maestro.friction' in sys.modules)"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert src == "False"
    from pathlib import Path
    assert "friction" not in (Path(__file__).parents[1] / "maestro" / "dispatcher.py").read_text()
