"""Compaction: pre-snapshot events move to archive; history and idempotency survive."""
import json

import pytest

from maestro import event_log, snapshot as snap_mod, store
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
