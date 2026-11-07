"""`steplog.session_digest` and `maestro logs --summary --session` over synthetic logs."""
from __future__ import annotations

import json

from maestro import cli, event_log, steplog, store
from maestro.view import rounds as rounds_view

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"


def _w(path, records, raw=()):
    lines = [json.dumps(r) for r in records] + list(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def _tu(name, **inp):
    return {"type": "tool_use", "id": f"id-{name}-{len(inp)}", "name": name, "input": inp}


def _asst(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _claude_log(path):
    return _w(path, [
        {"type": "system", "subtype": "init", "model": "m"},
        _asst({"type": "text", "text": "Step: Reading the code"}, _tu("Read", file_path="/x/SKILL.md"),
              _tu("Grep", pattern="foo")),
        _asst({"type": "text", "text": "Step: Editing files"}, _tu("Edit", file_path="/x/a.py"),
              _tu("Write", file_path="/x/b.py"), _tu("Edit", file_path="/x/a.py")),
        _asst(_tu("Bash", command=".venv/bin/python -m pytest -q"), _tu("Skill", skill="demo-skill"),
              _tu("Agent", description="helper")),
        _asst(_tu("Bash", command='git commit -q -m "T-1: add the widget"'),
              _tu("Bash", command="git push origin HEAD")),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "is_error": True,
             "content": f"\x1b[31mboom\x1b[0m token={FAKE_TOKEN}\x07"}]}},
        {"type": "system", "subtype": "api_retry"},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
         "result": "All done."},
    ], raw=["stderr noise line"])


def test_claude_digest_fields(tmp_path):
    d = steplog.session_digest(_claude_log(tmp_path / "reconcile-T-1-100.000000.stream.jsonl"))
    assert d["groups"] == {"understand": 4, "change": 3, "verify": 1, "ship": 2}
    assert d["files"] == ["/x/a.py", "/x/b.py"]
    assert d["commits"] == ["T-1: add the widget"]
    assert d["test_runs"] == 1 and d["pushed"] is True
    assert d["skills"] == ["demo-skill"]            # the SKILL.md Read is not a run
    assert d["agents"] == 1
    assert d["errors"] == 1 and "boom" in d["first_error"]
    assert d["retries"] == 2                        # api_retry + non-JSON stderr line
    assert d["terminal"] is True and d["final_text"] == "All done."
    assert [s["label"] for s in d["steps"]] == ["Reading the code", "Editing files"]
    assert d["steps"][0]["calls"] == {"understand": 2, "change": 0, "verify": 0, "ship": 0}
    assert d["steps"][1]["calls"] == {"understand": 2, "change": 3, "verify": 1, "ship": 2}


def test_excerpts_are_stripped_and_redacted(tmp_path):
    d = steplog.session_digest(_claude_log(tmp_path / "reconcile-T-1-100.000000.stream.jsonl"))
    assert "\x1b" not in d["first_error"] and "\x07" not in d["first_error"]
    assert FAKE_TOKEN not in d["first_error"] and "[redacted]" in d["first_error"]
    assert len(steplog.sanitize("x" * 1000)) <= 200


def test_skill_md_read_is_never_a_skill_run(tmp_path):
    p = _w(tmp_path / "reconcile-T-1-1.000000.stream.jsonl",
           [_asst(_tu("Read", file_path="/s/SKILL.md"), _tu("Bash", command="cat SKILL.md"))])
    assert steplog.session_digest(p)["skills"] == []


def _pi_log(path):
    def tool(i, name, **args):
        return {"type": "tool_execution_start", "toolCallId": f"c{i}", "toolName": name, "args": args}
    return _w(path, [
        {"type": "session", "id": "s1"},
        {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Step: Checking tests"}]}},
        tool(1, "read", path="/x/a.py"),
        tool(2, "edit", path="/x/a.py"),
        tool(3, "bash", command="npm test"),
        {"type": "tool_execution_end", "toolCallId": "c3", "toolName": "bash", "isError": True,
         "result": {"content": [{"type": "text", "text": "FAIL 1"}]}},
        tool(4, "bash", command="gh pr create --title x"),
        {"type": "auto_retry_start"},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                            "content": [{"type": "text", "text": "Finished."}]}},
        {"type": "agent_end"},
    ])


def test_pi_digest_fields(tmp_path):
    d = steplog.session_digest(_pi_log(tmp_path / "reconcile-T-1-100.000000.pi.jsonl"))
    assert d["groups"] == {"understand": 1, "change": 1, "verify": 1, "ship": 1}
    assert d["files"] == ["/x/a.py"] and d["test_runs"] == 1 and d["pr"] is True
    assert d["errors"] == 1 and d["first_error"] == "FAIL 1"
    assert d["retries"] == 1 and d["terminal"] is True and d["final_text"] == "Finished."
    assert d["steps"][0]["calls"]["verify"] == 1


def test_failed_start_and_unsupported(tmp_path):
    err = _w(tmp_path / "reconcile-T-1-1.000000.stream.jsonl",
             [{"type": "result", "is_error": True, "subtype": "error", "api_error_status": 401,
               "result": "Invalid API key"}])
    d = steplog.session_digest(err)
    assert d["failed_start"] is True and d["outcome"] == "error:auth"
    assert "failed to start: error:auth" in steplog.digest_trouble(d)
    oc = _w(tmp_path / "reconcile-T-1-2.000000.opencode.jsonl", [{"type": "text"}])
    assert steplog.format_session_digest(steplog.session_digest(oc)) == [
        "digest not supported for this runner"]


def test_cached_digest_memoizes_and_guards_runaway(tmp_path, monkeypatch):
    p = _claude_log(tmp_path / "reconcile-T-1-100.000000.stream.jsonl")
    calls = []
    real = steplog.session_digest
    monkeypatch.setattr(steplog, "session_digest", lambda path: calls.append(path) or real(path))
    rounds_view._DIGEST_CACHE.clear()
    rounds_view.cached_digest(p)
    rounds_view.cached_digest(p)
    assert len(calls) == 1
    monkeypatch.setattr(rounds_view, "RUNAWAY_BYTES", 10)
    assert "runaway output?" in rounds_view.cached_digest(p)["note"]


def test_logs_summary_session_prints_digest_with_window_events(home, capsys):
    key = "T-7"
    ev = event_log.append(home, key, "Note", {"text": "hello from the session"}, actor="reconciler")
    ts = store.iso_to_epoch(ev["ts"])
    log = home / "agent-logs" / key / f"reconcile-{key}-{ts - 5:.6f}.stream.jsonl"
    _claude_log(log)
    from maestro.sessions import list_sessions
    sess = list_sessions(home, key)[0]
    assert cli.main(["--home", str(home), "logs", key, "--summary", "--session", sess["session_id"]]) == 0
    out = capsys.readouterr().out
    assert "Understand 4 · Change 3 · Verify 1 · Ship 2" in out
    assert "event: Note" in out and "hello from the session" in out
    assert "Said: All done." in out
    assert FAKE_TOKEN not in out
    assert cli.main(["--home", str(home), "logs", key, "--summary", "--session", "nope"]) == 1
