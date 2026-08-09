"""Compaction: pre-snapshot events move to archive; history and idempotency survive."""
import dataclasses
import json
import os

import pytest

from maestro import event_log, snapshot as snap_mod, store
from maestro.dispatcher import run_compact_tick
from maestro.ops import compact


def _append_n(home, key, n, start=1):
    for i in range(start, start + n):
        event_log.append(home, key, "Note", {"n": i}, actor="t")


def _rebuild(home, key):
    return snap_mod.rebuild(home, key)


# ---------------------------------------------------------------------------
# Core: compaction moves events and read() still returns full history
# ---------------------------------------------------------------------------

def test_compact_moves_pre_snapshot_events(cfg):
    home = cfg.home
    _append_n(home, "T-1", 5)                        # seq 1-5
    snap = _rebuild(home, "T-1")                     # observed_seq = 5
    event_log.append(home, "T-1", "Note", {"n": 6}, actor="t")   # seq 6

    result = compact(cfg, "T-1")

    assert result["archived"] == 5
    assert result["remaining"] == 1
    assert result["cutoff_seq"] == 5

    # Archive has seq 1-5; active has only seq 6.
    archive = store.read_jsonl(store.events_archive_path(home, "T-1"))
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in archive] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in active] == [6]


def test_read_returns_full_history_after_compact(cfg):
    home = cfg.home
    _append_n(home, "T-1", 5)
    _rebuild(home, "T-1")
    event_log.append(home, "T-1", "Note", {"n": 6}, actor="t")

    compact(cfg, "T-1")

    all_events = event_log.read(home, "T-1")
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5, 6]


def test_compact_is_lossless(cfg):
    home = cfg.home
    _append_n(home, "T-1", 10)
    before = event_log.read(home, "T-1")
    _rebuild(home, "T-1")                            # snapshot at seq 10

    compact(cfg, "T-1")

    after = event_log.read(home, "T-1")
    assert before == after


# ---------------------------------------------------------------------------
# Step-id idempotency survives compaction
# ---------------------------------------------------------------------------

def test_step_id_dedup_survives_compaction(cfg):
    home = cfg.home
    event_log.append(home, "T-1", "PrOpened", {"number": 42},
                     actor="a", step_id="open-pr-T-1")
    _rebuild(home, "T-1")
    compact(cfg, "T-1")                              # step_id now only in archive

    # Attempting to re-append the same step_id must be a no-op.
    result = event_log.append(home, "T-1", "PrOpened", {"number": 99},
                               actor="b", step_id="open-pr-T-1")
    assert result is None

    prs = [e for e in event_log.read(home, "T-1") if e["type"] == "PrOpened"]
    assert len(prs) == 1
    assert prs[0]["payload"]["number"] == 42


# ---------------------------------------------------------------------------
# Idempotent re-compaction: running compact twice is safe
# ---------------------------------------------------------------------------

def test_double_compact_is_idempotent(cfg):
    home = cfg.home
    _append_n(home, "T-1", 5)
    _rebuild(home, "T-1")
    event_log.append(home, "T-1", "Note", {"n": 6}, actor="t")

    compact(cfg, "T-1")
    result2 = compact(cfg, "T-1")

    # Second compact sees the updated snapshot (observed_seq still 5), active has
    # only seq 6 which is > cutoff, so nothing to move.
    assert result2["archived"] == 0
    assert result2["remaining"] == 1

    all_events = event_log.read(home, "T-1")
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# No observed snapshot: compact does nothing
# ---------------------------------------------------------------------------

def test_compact_with_no_snapshot_moves_nothing(cfg):
    home = cfg.home
    _append_n(home, "T-1", 3)
    # No rebuild → observed_seq == 0

    result = compact(cfg, "T-1")

    assert result["archived"] == 0
    assert result["remaining"] == 3
    assert not store.events_archive_path(home, "T-1").exists() or \
           store.read_jsonl(store.events_archive_path(home, "T-1")) == []


# ---------------------------------------------------------------------------
# Incremental compaction across sweeps
# ---------------------------------------------------------------------------

def test_incremental_compact(cfg):
    home = cfg.home
    _append_n(home, "T-1", 3)
    _rebuild(home, "T-1")                            # observed_seq = 3
    compact(cfg, "T-1")                              # archive: 1-3, active: empty

    _append_n(home, "T-1", 3, start=4)              # seq 4-6 (new events)
    _rebuild(home, "T-1")                            # observed_seq = 6
    result = compact(cfg, "T-1")                    # archive: 1-6, active: empty

    assert result["archived"] == 3
    archive = store.read_jsonl(store.events_archive_path(home, "T-1"))
    assert [e["seq"] for e in archive] == [1, 2, 3, 4, 5, 6]
    all_events = event_log.read(home, "T-1")
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------------------
# RB-6: crash safety. Two windows can be hit by a process kill / power loss:
#   (a) during the archive append itself (before it is durable), and
#   (b) after the archive append is durable but before the active log is
#       replaced (the two files now hold the same events -- a duplicate, not a
#       loss). Both are proven by stubbing os.fsync to raise at the point a
#       real crash would land, exactly as the ticket's QA note prescribes.
# ---------------------------------------------------------------------------

def test_crash_during_archive_append_loses_nothing(cfg, monkeypatch):
    home = cfg.home
    _append_n(home, "T-1", 5)
    _rebuild(home, "T-1")                            # observed_seq = 5
    event_log.append(home, "T-1", "Note", {"n": 6}, actor="t")   # seq 6, stays active
    before = snap_mod.fold("T-1", event_log.read(home, "T-1"))

    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 1:               # the archive append's own fsync
            raise OSError("simulated crash during archive append")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        compact(cfg, "T-1")

    # The active log was never touched -- still fully readable, still folds to
    # exactly the pre-compaction snapshot.
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in active] == [1, 2, 3, 4, 5, 6]
    after = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    assert after == before


def test_crash_between_archive_append_and_replace_is_survivable(cfg, monkeypatch):
    home = cfg.home
    event_log.append(home, "T-1", "TicketCreated", {"title": "t", "spec_hash": "x"}, actor="d")  # seq 1
    event_log.append(home, "T-1", "Failed", {"error": "boom"}, actor="r")                          # seq 2
    _rebuild(home, "T-1")                             # observed_seq = 2, failure_count = 1
    event_log.append(home, "T-1", "Note", {"n": 3}, actor="t")                                     # seq 3, stays active
    before = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    assert before.failure_count == 1

    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 2:                # archive's fsync (#1) succeeds; the
            raise OSError("simulated crash between archive append and active-log replace")
        return real_fsync(fd)

    monkeypatch.setattr(store.os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        compact(cfg, "T-1")

    # Archive holds the pre-cutoff events durably; the active log was never
    # replaced, so the same events are duplicated there too.
    archive = store.read_jsonl(store.events_archive_path(home, "T-1"))
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in archive] == [1, 2]
    assert [e["seq"] for e in active] == [1, 2, 3]

    # event_log.read dedups by seq -- union of archive + active still contains
    # every event exactly once, and the fold matches the pre-compaction
    # snapshot: failure_count is NOT inflated by the duplicated Failed event.
    all_events = event_log.read(home, "T-1")
    assert [e["seq"] for e in all_events] == [1, 2, 3]
    after = snap_mod.fold("T-1", all_events)
    assert after == before
    assert after.failure_count == 1


def test_duplicate_seq_across_archive_and_active_is_deduped_by_read(cfg):
    """Directly exercises event_log.read's dedup, independent of how the
    duplicate got there -- the survivable shape a crash mid-compact leaves."""
    home = cfg.home
    _append_n(home, "T-1", 3)
    archive_path = store.events_archive_path(home, "T-1")
    active = store.read_jsonl(store.events_path(home, "T-1"))
    store.append_line(archive_path, "\n".join(json.dumps(e, separators=(",", ":")) for e in active))

    events = event_log.read(home, "T-1")
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_compact_survives_crash_via_real_maestro_compact_verb(cfg, monkeypatch):
    """QA per CLAUDE.md: drive the real ``maestro compact`` verb (not internal
    calls), inject the crash, then prove readability + fold via the same verb
    surface an agent/human would use."""
    from maestro import cli

    home = cfg.home
    _append_n(home, "T-1", 5)
    _rebuild(home, "T-1")
    event_log.append(home, "T-1", "Note", {"n": 6}, actor="t")
    before = snap_mod.fold("T-1", event_log.read(home, "T-1"))

    real_fsync = os.fsync
    monkeypatch.setattr(store.os, "fsync",
                         lambda fd: (_ for _ in ()).throw(OSError("simulated crash")))
    monkeypatch.setenv("MAESTRO_HOME", str(home))

    with pytest.raises(OSError):
        cli.main(["compact", "T-1"])

    monkeypatch.setattr(store.os, "fsync", real_fsync)
    after = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    assert after == before

    # Now let compaction actually complete for a fresh key, and prove a real
    # dispatcher sweep (run_compact_tick) drives the same verb end-to-end
    # without loss -- not just the direct `maestro compact` invocation above.
    _append_n(home, "T-2", 5)
    _rebuild(home, "T-2")
    event_log.append(home, "T-2", "Note", {"n": 6}, actor="t")
    before2 = snap_mod.fold("T-2", event_log.read(home, "T-2"))

    cfg2 = dataclasses.replace(cfg, compact_interval=1, compact_min_events=1)
    result = run_compact_tick(cfg2, now=1000.0)
    assert "T-2" in result["compacted"]
    archive = store.read_jsonl(store.events_archive_path(home, "T-2"))
    assert [e["seq"] for e in archive] == [1, 2, 3, 4, 5]
    after2 = snap_mod.fold("T-2", event_log.read(home, "T-2"))
    assert after2 == before2
