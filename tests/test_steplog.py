"""IMPL_STEP folding: idempotency, snapshot updates, and compact integration."""
import json
from pathlib import Path

import pytest

from maestro import event_log, snapshot as snap_mod, store
from maestro import events as E
from maestro.steplog import classify_result, fold_stream, fold_current_session, session_outcome
from maestro.ops import compact

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_stream(tmp_path: Path, session_id: str, tool_uses: list[dict]) -> Path:
    """Write a minimal stream.jsonl with the given tool_use blocks."""
    p = tmp_path / f"reconcile-T-1-{session_id}.stream.jsonl"
    lines = [
        {"type": "system", "subtype": "init", "session_id": session_id},
    ]
    for i, tu in enumerate(tool_uses):
        lines.append({
            "type": "assistant",
            "message": {
                "id": f"msg_{i:04d}",
                "role": "assistant",
                "content": [{"type": "tool_use", **tu}],
            },
        })
    p.write_text("\n".join(json.dumps(obj) for obj in lines) + "\n", encoding="utf-8")
    return p


def _bash(tool_id: str, command: str, description: str = "") -> dict:
    return {"id": tool_id, "name": "Bash", "input": {"command": command, "description": description}}


def _edit(tool_id: str, file_path: str) -> dict:
    return {"id": tool_id, "name": "Edit", "input": {"file_path": file_path, "old_string": "x", "new_string": "y"}}


def _agent(tool_id: str, description: str) -> dict:
    return {"id": tool_id, "name": "Agent", "input": {"description": description}}


# ---------------------------------------------------------------------------
# Basic folding
# ---------------------------------------------------------------------------

def test_fold_stream_appends_impl_step_events(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "make test", "Run tests"),
        _edit("tu_002", "maestro/ops.py"),
    ])
    n = fold_stream(home, "T-1", stream)
    assert n == 2
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2
    kinds = {e["payload"]["kind"] for e in evs}
    assert kinds == {"command", "edit"}


def test_fold_stream_summary_and_tool(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _edit("tu_001", "maestro/events.py"),
    ])
    fold_stream(home, "T-1", stream)
    ev = next(e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP)
    p = ev["payload"]
    assert p["tool"] == "Edit"
    assert p["kind"] == "edit"
    assert "maestro/events.py" in p["summary"]


def test_fold_stream_bash_with_gh_pr_is_pr_kind(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "gh pr create --title 'x' --body 'y'"),
    ])
    fold_stream(home, "T-1", stream)
    ev = next(e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP)
    assert ev["payload"]["kind"] == "pr"


def test_fold_stream_agent_tool_is_subagent_kind(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _agent("tu_001", "Run the test suite"),
    ])
    fold_stream(home, "T-1", stream)
    ev = next(e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP)
    assert ev["payload"]["kind"] == "subagent"
    assert ev["payload"]["tool"] == "Agent"


# ---------------------------------------------------------------------------
# Idempotency: re-folding the same stream appends nothing new
# ---------------------------------------------------------------------------

def test_refold_same_stream_is_idempotent(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "pytest"),
        _edit("tu_002", "foo.py"),
    ])
    fold_stream(home, "T-1", stream)
    n2 = fold_stream(home, "T-1", stream)
    assert n2 == 0  # all step_ids already present
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2


# ---------------------------------------------------------------------------
# Snapshot updates: impl_turns and last_step
# ---------------------------------------------------------------------------

def test_snapshot_tracks_impl_turns_and_last_step(home, tmp_path):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "pytest", "Run tests"),
        _edit("tu_002", "maestro/cli.py"),
    ])
    fold_stream(home, "T-1", stream)
    snap = snap_mod.rebuild(home, "T-1")
    assert snap.impl_turns >= 1
    assert snap.last_step is not None


# ---------------------------------------------------------------------------
# Compact: IMPL_STEP events archive correctly; snapshot is unchanged
# ---------------------------------------------------------------------------

def test_compact_archives_impl_step_events(home, tmp_path, cfg):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "pytest"),
        _edit("tu_002", "foo.py"),
    ])
    fold_stream(home, "T-1", stream)
    snap_before = snap_mod.rebuild(home, "T-1")

    result = compact(cfg, "T-1")

    assert result["archived"] >= 2
    # All IMPL_STEP events are accessible via read() (archive + active)
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2

    # Snapshot is deterministic: rebuilding after compact gives same data
    snap_after = snap_mod.rebuild(home, "T-1")
    assert snap_after.impl_turns == snap_before.impl_turns
    assert snap_after.last_step == snap_before.last_step


def test_step_id_dedup_survives_compact(home, tmp_path, cfg):
    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "pytest"),
    ])
    fold_stream(home, "T-1", stream)
    snap_mod.rebuild(home, "T-1")
    compact(cfg, "T-1")

    # Re-fold after compact — step_ids now only in archive, must still dedup
    n = fold_stream(home, "T-1", stream)
    assert n == 0


# ---------------------------------------------------------------------------
# fold_current_session: safe no-op when no claim exists
# ---------------------------------------------------------------------------

def test_fold_current_session_no_claim_is_noop(home):
    n = fold_current_session(home, "T-1")
    assert n == 0


# ---------------------------------------------------------------------------
# CLI integration: maestro fold-steps
# ---------------------------------------------------------------------------

def test_cli_fold_steps_with_log_arg(home, tmp_path):
    from maestro.cli import main

    stream = _write_stream(tmp_path, "sess1", [
        _bash("tu_001", "make install"),
    ])
    rc = main(["--home", str(home), "fold-steps", "T-1", "--log", str(stream)])
    assert rc == 0
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 1


def test_cli_fold_steps_with_opencode_log_arg_is_idempotent(home, tmp_path):
    """AC1: real `maestro fold-steps <KEY>` over a captured opencode log appends
    one ImplStep per tool call; a second run folds 0."""
    from maestro.cli import main

    log = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="make install"),
        _oc_step_finish(),
    ])
    rc = main(["--home", str(home), "fold-steps", "T-1", "--log", str(log)])
    assert rc == 0
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 1

    rc2 = main(["--home", str(home), "fold-steps", "T-1", "--log", str(log)])
    assert rc2 == 0
    evs2 = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs2) == 1


# ---------------------------------------------------------------------------
# classify_result: is_error/api_error_status drive the verdict, not subtype
# ---------------------------------------------------------------------------

def test_classify_result_rate_limited_for_real_incident_payload():
    """The 2026-07-19 runaway payload: subtype says success, is_error says otherwise."""
    obj = {
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "terminal_reason": "api_error",
    }
    assert classify_result(obj)["outcome"] == "rate_limited"


def test_classify_result_success_for_real_healthy_payload():
    """api_error_status present-but-null must not trip a naive key-presence check."""
    obj = {"subtype": "success", "is_error": False, "api_error_status": None}
    assert classify_result(obj)["outcome"] == "success"


def test_classify_result_error_during_execution_fallback():
    """A non-success subtype with no is_error/api_error_status still classifies as error."""
    obj = {"subtype": "error_during_execution"}
    assert classify_result(obj)["outcome"] == "error"


def test_classify_result_non_429_error_status_is_plain_error():
    obj = {"subtype": "success", "is_error": True, "api_error_status": 500}
    assert classify_result(obj)["outcome"] == "error"


# ---------------------------------------------------------------------------
# session_outcome: tail-scan a stream log for terminal result + rate_limit_event
# ---------------------------------------------------------------------------

def test_session_outcome_rate_limited_fixture():
    assert session_outcome(FIXTURES / "rate_limited.stream.jsonl")["outcome"] == "rate_limited"


def test_session_outcome_advisory_rate_limit_event_does_not_override_success():
    """A carried-but-ignored rate_limit_event is context, never the verdict."""
    outcome = session_outcome(FIXTURES / "rate_limit_advisory_only.stream.jsonl")
    assert outcome["outcome"] == "success"
    assert outcome["rate_limit_info"]["overageStatus"] == "allowed"


def test_session_outcome_clean_success_fixture():
    assert session_outcome(FIXTURES / "sample.stream.jsonl")["outcome"] == "success"


def test_session_outcome_running_when_no_terminal_result(tmp_path):
    p = tmp_path / "reconcile-T-1-1.stream.jsonl"
    p.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n",
        encoding="utf-8",
    )
    assert session_outcome(p)["outcome"] == "running"


def test_session_outcome_still_running_with_no_result_and_no_pid_given(tmp_path):
    """Backward compat: omitting `pid` (the default) never classifies as
    crashed, even with only garbage output -- unchanged behavior for every
    existing caller that doesn't know a pid."""
    p = tmp_path / "reconcile-T-1-2.stream.jsonl"
    p.write_text("zsh: command not found: claude\n", encoding="utf-8")
    assert session_outcome(p)["outcome"] == "running"


def test_session_outcome_still_running_with_no_result_and_a_live_pid(tmp_path):
    """A pid that's alive (this test process itself) means the session could
    still be mid-flight -- garbage-only output alone is not enough."""
    import os
    p = tmp_path / "reconcile-T-1-3.stream.jsonl"
    p.write_text("zsh: command not found: claude\n", encoding="utf-8")
    assert session_outcome(p, pid=os.getpid())["outcome"] == "running"


def test_session_outcome_crashed_when_only_garbage_output_and_pid_is_dead(tmp_path):
    """T-89 (Gap 3, AC4): a session whose log holds only non-JSON runner
    output (the stderr-splat shape `sessions.py` Popens stdout+stderr into)
    and whose pid is provably dead classifies as `crashed`, not `running`
    forever -- carrying a bounded tail of the log."""
    p = tmp_path / "reconcile-T-1-4.stream.jsonl"
    p.write_text("zsh: command not found: claude\nno network\n", encoding="utf-8")
    result = session_outcome(p, pid=_dead_pid())
    assert result["outcome"] == "crashed"
    assert "no network" in result["result"]["tail"]


def test_session_outcome_crashed_tail_is_bounded(tmp_path):
    p = tmp_path / "reconcile-T-1-5.stream.jsonl"
    p.write_text("\n".join(f"garbage line {i}" for i in range(200)) + "\n", encoding="utf-8")
    result = session_outcome(p, pid=_dead_pid())
    assert result["outcome"] == "crashed"
    tail_lines = result["result"]["tail"].splitlines()
    assert len(tail_lines) <= 20
    assert tail_lines[-1] == "garbage line 199"


def test_session_outcome_a_valid_json_line_among_garbage_never_crashes(tmp_path):
    """Some real JSON was written (just no terminal `result` yet) -- this is
    genuinely still running, not crashed, regardless of pid liveness."""
    p = tmp_path / "reconcile-T-1-6.stream.jsonl"
    p.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
        "some stray stderr text\n",
        encoding="utf-8",
    )
    assert session_outcome(p, pid=_dead_pid())["outcome"] == "running"


def _dead_pid() -> int:
    """A pid `claims.pid_alive` reliably reports dead without shelling out or
    touching a real process -- see its own `pid <= 0` guard."""
    return -1


def test_session_outcome_unknown_for_plain_text_log(tmp_path):
    p = tmp_path / "reconcile-T-1-1.log"
    p.write_text("plain text output\n", encoding="utf-8")
    assert session_outcome(p)["outcome"] == "unknown"


def test_session_outcome_unknown_for_a_grammar_neither_suffix_recognizes(tmp_path):
    """A suffix that is neither Claude's '.stream.jsonl' nor opencode's
    '.opencode.jsonl' (OC-5 made the latter parseable) stays 'unknown'."""
    p = tmp_path / "reconcile-T-1-1.weird.jsonl"
    p.write_text(json.dumps({"type": "result", "subtype": "success"}) + "\n", encoding="utf-8")
    assert session_outcome(p)["outcome"] == "unknown"


# ---------------------------------------------------------------------------
# OC-5: opencode log folding and session_outcome
# ---------------------------------------------------------------------------

def _write_opencode(tmp_path: Path, key: str, epoch: str, records: list[dict]) -> Path:
    p = tmp_path / f"reconcile-{key}-{epoch}.opencode.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _oc_step_start() -> dict:
    return {"type": "step_start", "part": {}}


def _oc_tool_use(call_id: str, tool: str, **input_kv) -> dict:
    part = {"tool": tool, "callID": call_id}
    if input_kv:
        part["state"] = {"status": "completed", "input": input_kv}
    return {"type": "tool_use", "part": part}


def _oc_text(text: str) -> dict:
    return {"type": "text", "part": {"text": text}}


def _oc_step_finish(reason: str = "stop") -> dict:
    return {"type": "step_finish", "part": {"reason": reason, "cost": 0, "tokens": {"input": 1, "output": 1}}}


def test_fold_opencode_stream_appends_impl_step_and_is_idempotent(home, tmp_path):
    log = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="pytest"),
        _oc_tool_use("call_2", "edit", filePath="maestro/ops.py"),
        _oc_step_finish(),
    ])
    n = fold_stream(home, "T-1", log)
    assert n == 2
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2
    kinds = {e["payload"]["kind"] for e in evs}
    assert kinds == {"command", "edit"}

    n2 = fold_stream(home, "T-1", log)
    assert n2 == 0
    evs2 = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs2) == 2


def test_fold_opencode_cross_session_callid_reuse_yields_distinct_steps(home, tmp_path):
    """AC2: two different opencode sessions that reuse a callID (the payload's
    own callID uniqueness is UNVERIFIED per spec Notes) must not collapse into
    one ImplStep -- the session discriminator comes from the filename epoch."""
    log1 = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="pytest"),
        _oc_step_finish(),
    ])
    log2 = _write_opencode(tmp_path, "T-1", "2000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="make test"),
        _oc_step_finish(),
    ])
    fold_stream(home, "T-1", log1)
    fold_stream(home, "T-1", log2)
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2
    assert len({e["step_id"] for e in evs}) == 2


def test_opencode_session_outcome_success_from_step_finish_reason(tmp_path):
    log = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="pytest"),
        _oc_step_finish("stop"),
    ])
    assert session_outcome(log)["outcome"] == "success"


def test_opencode_session_outcome_error_from_step_finish_reason(tmp_path):
    log = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="pytest"),
        _oc_step_finish("error"),
    ])
    assert session_outcome(log)["outcome"] == "error"


def test_opencode_session_outcome_running_while_no_terminal_record(tmp_path):
    log = _write_opencode(tmp_path, "T-1", "1000.000000", [
        _oc_step_start(),
        _oc_tool_use("call_1", "bash", command="pytest"),
    ])
    assert session_outcome(log)["outcome"] == "running"


# ---------------------------------------------------------------------------
# T-58: pi log folding and session_outcome -- record shapes below are the
# REAL, verified `pi --mode json` vocabulary (docs/json.md in
# @earendil-works/pi-coding-agent), captured live and reproduced here as
# hand-composed records for the arithmetic/idempotency tests; the two
# classification tests (AC3/AC9's own error-log requirement) instead read the
# genuinely committed real captures under tests/fixtures/*.pi.jsonl -- see
# spec Notes' "do not hand-author" instruction.
# ---------------------------------------------------------------------------

def _write_pi(tmp_path: Path, key: str, epoch: str, records: list[dict]) -> Path:
    p = tmp_path / f"reconcile-{key}-{epoch}.pi.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _pi_session(session_id: str) -> dict:
    return {"type": "session", "version": 3, "id": session_id,
            "timestamp": "2026-08-14T00:00:00.000Z", "cwd": "/tmp"}


def _pi_turn_start() -> dict:
    return {"type": "turn_start"}


def _pi_tool_start(call_id: str, tool: str, **args_kv) -> dict:
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool,
            "args": args_kv}


def _pi_message_end(*, role: str = "assistant", stop_reason: str = "stop",
                     error_message: str | None = None, provider: str = "anthropic") -> dict:
    msg = {"role": role, "stopReason": stop_reason, "provider": provider, "content": []}
    if error_message:
        msg["errorMessage"] = error_message
    return {"type": "message_end", "message": msg}


def _pi_agent_end() -> dict:
    return {"type": "agent_end", "messages": [], "willRetry": False}


def test_fold_pi_stream_appends_impl_step_and_is_idempotent(home, tmp_path):
    log = _write_pi(tmp_path, "T-1", "1000.000000", [
        _pi_session("s1"),
        _pi_turn_start(),
        _pi_tool_start("call_1", "bash", command="pytest"),
        _pi_message_end(),
        _pi_turn_start(),
        _pi_tool_start("call_2", "edit", path="maestro/ops.py"),
        _pi_message_end(),
        _pi_agent_end(),
    ])
    n = fold_stream(home, "T-1", log)
    assert n == 2
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2
    kinds = {e["payload"]["kind"] for e in evs}
    assert kinds == {"command", "edit"}

    n2 = fold_stream(home, "T-1", log)
    assert n2 == 0
    evs2 = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs2) == 2


def test_fold_pi_cross_session_toolcallid_reuse_yields_distinct_steps(home, tmp_path):
    """Two different pi sessions (distinct real per-invocation ``session.id``s)
    that happen to reuse a toolCallId must not collapse into one ImplStep."""
    log1 = _write_pi(tmp_path, "T-1", "1000.000000", [
        _pi_session("sess-aaa"),
        _pi_turn_start(),
        _pi_tool_start("call_1", "bash", command="pytest"),
        _pi_message_end(),
        _pi_agent_end(),
    ])
    log2 = _write_pi(tmp_path, "T-1", "2000.000000", [
        _pi_session("sess-bbb"),
        _pi_turn_start(),
        _pi_tool_start("call_1", "bash", command="make test"),
        _pi_message_end(),
        _pi_agent_end(),
    ])
    fold_stream(home, "T-1", log1)
    fold_stream(home, "T-1", log2)
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 2
    assert len({e["step_id"] for e in evs}) == 2


def test_cli_fold_steps_with_pi_log_arg_is_idempotent(home, tmp_path):
    """AC5: real `maestro fold-steps <KEY>` over a pi log appends one ImplStep
    per tool call; a second run folds 0."""
    from maestro.cli import main

    log = _write_pi(tmp_path, "T-1", "1000.000000", [
        _pi_session("s1"),
        _pi_turn_start(),
        _pi_tool_start("call_1", "bash", command="make install"),
        _pi_message_end(),
        _pi_agent_end(),
    ])
    rc = main(["--home", str(home), "fold-steps", "T-1", "--log", str(log)])
    assert rc == 0
    evs = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs) == 1

    rc2 = main(["--home", str(home), "fold-steps", "T-1", "--log", str(log)])
    assert rc2 == 0
    evs2 = [e for e in event_log.read(home, "T-1") if e["type"] == E.IMPL_STEP]
    assert len(evs2) == 1


def test_pi_session_outcome_success_from_real_captured_fixture():
    """AC3: a real captured `pi --mode json` success stream (a clean bash tool
    call, terminal stopReason "stop") classifies as success."""
    outcome = session_outcome(FIXTURES / "sample.pi.jsonl")
    assert outcome["outcome"] == "success"
    assert outcome["provider"] == "ollama"


def test_pi_session_outcome_error_from_real_captured_401_fixture():
    """AC3: a real captured `pi --mode json` stream against an invalid API key
    -- the verified exit-code trap (spec Notes): pi exits 0 in json mode even
    on a 401, so this must classify from the record stream (stopReason
    "error" + errorMessage), never from a process exit code maestro never
    reads anyway."""
    outcome = session_outcome(FIXTURES / "error.pi.jsonl")
    assert outcome["outcome"] == "error"
    assert outcome["provider"] == "anthropic"
    assert "401" in outcome["result"]["result"]


def test_pi_session_outcome_running_while_no_terminal_record(tmp_path):
    log = _write_pi(tmp_path, "T-1", "1000.000000", [
        _pi_session("s1"),
        _pi_turn_start(),
        _pi_tool_start("call_1", "bash", command="pytest"),
    ])
    assert session_outcome(log)["outcome"] == "running"


def test_pi_session_outcome_truncated_final_line_is_running_not_error(tmp_path):
    """AC4: a pi log truncated mid-write (process killed before its final
    line -- the real captured error fixture's own agent_end line -- was fully
    flushed) classifies as running, not error, and the parser does not raise."""
    real = (FIXTURES / "error.pi.jsonl").read_text(encoding="utf-8")
    lines = real.rstrip("\n").split("\n")
    truncated = "\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2]
    p = tmp_path / "reconcile-T-1-1000.000000.pi.jsonl"
    p.write_text(truncated, encoding="utf-8")

    outcome = session_outcome(p)  # must not raise

    assert outcome["outcome"] == "running"


def test_pi_session_outcome_line_broken_mid_json_is_skipped_not_raised(tmp_path):
    """AC4 counterpart: a final line that IS newline-terminated but corrupt
    mid-JSON (a writer flushed a torn buffer) hits the existing
    ``JSONDecodeError: continue`` path, not a crash -- and still classifies
    as running since the (unparseable) agent_end record was never seen."""
    real = (FIXTURES / "error.pi.jsonl").read_text(encoding="utf-8")
    lines = real.rstrip("\n").split("\n")
    torn = lines[-1][: len(lines[-1]) // 2] + '"garbage'
    p = tmp_path / "reconcile-T-1-2000.000000.pi.jsonl"
    p.write_text("\n".join(lines[:-1]) + "\n" + torn + "\n", encoding="utf-8")

    outcome = session_outcome(p)  # must not raise

    assert outcome["outcome"] == "running"


def test_pi_session_outcome_unknown_for_a_grammar_neither_suffix_recognizes(tmp_path):
    p = tmp_path / "reconcile-T-1-1.weird.jsonl"
    p.write_text(json.dumps({"type": "agent_end", "messages": []}) + "\n", encoding="utf-8")
    assert session_outcome(p)["outcome"] == "unknown"
