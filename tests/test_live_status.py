"""T-226: `round_label` per round/phase and `steplog.live_status` over synthetic logs."""
from __future__ import annotations

import json
import os


from conftest import seed_phase
from maestro import event_log, snapshot as snap_mod, steplog
from maestro.view import board as board_view, rounds
from maestro.config import Config
from maestro import claims


def _phase(home, key, phase, reason=""):
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": reason}, actor="r")
    snap_mod.rebuild(home, key)


def _to_implementing(home, key, prev, reason):
    seed_phase(home, key)
    if prev != "ready":
        _phase(home, key, prev)
    _phase(home, key, "implementing", reason)


def test_fresh_and_unknown(home):
    _to_implementing(home, "T-1", "ready", "worktree ready")
    assert rounds.round_label(home, "T-1") == "Implementing the ticket"
    _to_implementing(home, "T-2", "awaiting-human", "human said hi")
    assert rounds.round_label(home, "T-2") == "Implementing (your message)"


def test_qa_fix_names_latest_failing_ac(home):
    _to_implementing(home, "T-1", "ready", "worktree ready")
    for i, verdict in ((1, "fail"), (3, "fail"), (4, "pass")):
        event_log.append(home, "T-1", "AcQaVerdict",
                         {"ac_index": i, "verdict": verdict, "ac_hash": f"h{i}"}, actor="q")
    _phase(home, "T-1", "qa")
    assert rounds.round_label(home, "T-1") == "QA · checking 1 AC"
    _phase(home, "T-1", "implementing", "qa failed: AC 3")
    assert rounds.round_label(home, "T-1") == "Fixing QA failure · AC 3"


def test_ci_fix_review_conflict_drift_split(home):
    seed_phase(home, "T-1")
    event_log.append(home, "T-1", "CiObserved",
                     {"state": "failure", "failing_checks": ["unit-tests"]}, actor="d")
    _phase(home, "T-1", "implementing", "CI failing: unit-tests")
    assert rounds.round_label(home, "T-1") == "Fixing CI · unit-tests"

    seed_phase(home, "T-2")
    _phase(home, "T-2", "in-review")
    for cid, author in ((1, "alice"), (2, "ci[bot]"), (3, "bob")):
        event_log.append(home, "T-2", "ReviewFeedbackReceived",
                         {"comment_id": cid, "state": "COMMENTED", "body": "x", "author": author},
                         actor="d")
    _phase(home, "T-2", "implementing", "review comment: 2 new")
    assert rounds.round_label(home, "T-2") == "Addressing review feedback · 2 comments"

    seed_phase(home, "T-3")
    _phase(home, "T-3", "implementing", "merge conflict with main")
    assert rounds.round_label(home, "T-3") == "Resolving merge conflict"

    seed_phase(home, "T-4")
    _phase(home, "T-4", "implementing", "base main advanced")
    assert rounds.round_label(home, "T-4") == "Syncing with base"

    seed_phase(home, "T-5")
    _phase(home, "T-5", "awaiting-human")
    _phase(home, "T-5", "implementing", "pr split decision: approved")
    assert rounds.round_label(home, "T-5") == "Splitting the PR"


def test_other_phases(home):
    for phase, label in (("triaging", "Triaging"), ("researching", "Researching"),
                         ("ready", "Setting up"), ("awaiting-human", "Applying your answer")):
        seed_phase(home, f"T-{phase}")
        _phase(home, f"T-{phase}", phase)
        assert rounds.round_label(home, f"T-{phase}") == label
    event_log.append(home, "T-ready", "PostQaSkillSpawned", {"skill": "s", "tree_key": "t"}, actor="d")
    assert rounds.round_label(home, "T-ready") == "Polishing the PR"


# --- live_status -------------------------------------------------------------

def _w(path, *recs):
    with path.open("a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _claude_text(text, ts=None):
    r = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    return {**r, "timestamp": ts} if ts else r


def _claude_tool(tid, cmd):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}}


def _claude_result(tid):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _pi_text(text):
    return {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_claude_step_beats_clause_and_tool_in_flight(tmp_path):
    log = tmp_path / "a.stream.jsonl"
    _w(log, _claude_text("Step: Wiring the board\nLet me read the file:"), _claude_tool("t1", "bzl test"))
    status, _ = steplog.live_status(log)
    assert status["step"] == "Wiring the board"
    assert status["clause"] == "read the file"
    assert status["tool"]["name"] == "Bash"
    now = status["tool"]["started"] + 220
    doing, now_text = steplog.live_texts(status, now)
    assert doing == "Wiring the board" and now_text.endswith("· 3m40s")
    assert "bzl test" in now_text


def test_clause_without_step_and_tool_finished(tmp_path):
    log = tmp_path / "a.stream.jsonl"
    _w(log, _claude_tool("t1", "ls"), _claude_result("t1"), _claude_text("Now I'll reply to the inline comment in its thread."))
    status, _ = steplog.live_status(log)
    assert status["tool"] is None
    doing, now_text = steplog.live_texts(status, status["last_ts"] + 5)
    assert doing == now_text == "reply to the inline comment in its thread"


def test_quiet_after_old_record(tmp_path):
    log = tmp_path / "a.stream.jsonl"
    _w(log, _claude_text("Step: Thinking"))
    old = 1_700_000_000
    os.utime(log, (old, old))
    status, _ = steplog.live_status(log)
    assert steplog.live_texts(status, old + 6 * 60 + 5)[1] == "quiet 6m"


def test_pi_log_tool_in_flight_and_noise_skipped(tmp_path):
    log = tmp_path / "a.pi.jsonl"
    _w(log, {"type": "session", "id": "s"}, _pi_text("Step: Editing the view"),
       {"type": "message_update", "x": 1},
       {"type": "tool_execution_start", "toolCallId": "p1", "toolName": "bash", "args": {"command": "make t"}})
    status, state = steplog.live_status(log)
    assert status["step"] == "Editing the view" and status["tool"]["name"] == "bash"
    _w(log, {"type": "tool_execution_end", "toolCallId": "p1"})
    status, _ = steplog.live_status(log, state)
    assert status["tool"] is None


def test_second_call_reads_only_new_bytes(tmp_path, monkeypatch):
    log = tmp_path / "a.stream.jsonl"
    _w(log, _claude_text("Step: One"))
    _, state = steplog.live_status(log)
    first_end = state["offset"]
    _w(log, _claude_text("Step: Two"))
    starts = []
    real = steplog.iter_records
    monkeypatch.setattr(steplog, "iter_records",
                        lambda p, *, start=0, **kw: (starts.append(start), real(p, start=start, **kw))[1])
    status, state = steplog.live_status(log, state)
    assert starts == [first_end] and status["step"] == "Two"
    starts.clear()
    steplog.live_status(log, state)  # unchanged file: memoized, no read
    assert starts == []


def test_board_now_only_for_running_keys(home, tmp_path):
    for k in ("T-1", "T-2"):
        seed_phase(home, k)
        _phase(home, k, "implementing", "worktree ready")
    log = tmp_path / "a.stream.jsonl"
    _w(log, _claude_text("Step: Doing <b>things</b>"))
    claims.write_claim(home, "T-1", os.getpid(), "t", log_path=str(log))
    rows = {r.key: r for r in board_view.board(home, Config(home=home), "all").rows}
    assert rows["T-1"].now == "Doing <b>things</b>"
    assert rows["T-2"].now == ""
