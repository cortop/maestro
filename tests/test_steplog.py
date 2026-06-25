"""IMPL_STEP folding: idempotency, snapshot updates, and compact integration."""
import json
from pathlib import Path

import pytest

from maestro import event_log, snapshot as snap_mod, store
from maestro import events as E
from maestro.steplog import fold_stream, fold_current_session
from maestro.ops import compact


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
