"""RB-11: crash-injection harness over the append and compact paths.

DESIGN.md's claims about the event log -- single writer, fencing, step-id
idempotency, crash-and-respawn safety -- are all claims about what happens
when a writer dies at the wrong moment. This file drives the REAL
`event_log.append`, `ops.compact` and `snapshot.rebuild` through the
`FaultInjector` shim (`tests/fault_injection.py`), which fakes only
`store.py`'s OS boundary (`os.fsync`, `os.replace`, `fcntl.flock`, the raw
line write) -- never the component under test.

THE INVARIANT (stated once, reused by every log-level test below): the union
of archive + active log is fully parseable, contains no reused seq, folds to
a snapshot that is either the pre-operation or the post-operation snapshot
(never a third thing), and every step-id recorded before the fault is still
visible to `event_log._scan_tail`. `updated_ts` is excluded from the pre/post
comparison -- it is a wall-clock artifact of *when* an event was folded, not
part of the crash-atomicity contract this ticket is proving.

RB-1's and RB-6's own targeted crash tests are not duplicated here:
- RB-6's three compact tests (`tests/test_compact.py`) now arm their crash via
  this same `faults` fixture instead of a hand-rolled `flaky_fsync` closure --
  folded into this harness's shim, per the ticket's own suggestion.
- RB-1's tests (`tests/test_event_log.py`, `tests/test_store.py`) exercise
  `append_line`'s recovery given a *pre-existing* torn tail (a hand-crafted
  fixture); `test_append_killed_mid_line...` below is the live-crash version
  of the same claim -- the torn tail is produced by an actual injected
  mid-write crash rather than a manually written fragment. Both are kept:
  they prove different things (recovery-from-artifact vs. crash-during-call).
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from maestro import event_log, ops, snapshot as snap_mod, store
from fault_injection import InjectedCrash


# ---------------------------------------------------------------------------
# The shared invariant
# ---------------------------------------------------------------------------

def _no_ts(snap):
    """`updated_ts` is a wall-clock artifact of injection timing, not part of
    the crash-atomicity contract -- normalize it away before comparing."""
    return dataclasses.replace(snap, updated_ts=None)


def assert_crash_invariant(home, key, *, pre, post, prior_step_ids):
    """THE invariant every injected-fault test proves. See module docstring."""
    archive_raw = store.read_jsonl(store.events_archive_path(home, key))
    active_raw = store.read_jsonl(store.events_path(home, key))

    # Parseable + no reused seq: a seq appearing in both files (RB-6's
    # survivable duplicate-relocation window) must be a byte-identical copy,
    # never two different events sharing one seq.
    by_seq: dict[int, set[str]] = {}
    for ev in archive_raw + active_raw:
        seq = ev.get("seq")
        if isinstance(seq, int):
            by_seq.setdefault(seq, set()).add(json.dumps(ev, sort_keys=True))
    for seq, reprs in by_seq.items():
        assert len(reprs) == 1, f"seq {seq} reused for two different events: {reprs}"

    events = event_log.read(home, key)  # parses cleanly, dedups by seq
    after = _no_ts(snap_mod.fold(key, events))
    assert after in (_no_ts(pre), _no_ts(post)), (
        f"fold after fault is neither pre- nor post-operation: {after} "
        f"(pre={_no_ts(pre)}, post={_no_ts(post)})"
    )

    _, step_ids = event_log._scan_tail(store.events_path(home, key))
    missing = prior_step_ids - step_ids
    assert not missing, f"step-ids present before the fault vanished: {missing}"


def _expect_event(seq, type_, payload, actor, step_id=None):
    """Build the event dict a clean `event_log.append` with these arguments
    would have produced -- everything but `ts` is deterministic, and `ts` is
    normalized away by `_no_ts` before any comparison uses this."""
    return {
        "seq": seq, "ts": "0", "key": "irrelevant-not-compared", "actor": actor,
        "type": type_, "step_id": step_id, "fencing_token": seq, "payload": payload,
    }


# ---------------------------------------------------------------------------
# append_line windows
# ---------------------------------------------------------------------------

def test_append_killed_mid_line_does_not_corrupt_or_swallow_next_append(home, faults):
    """RB-1's case, generalized: the crash that produces the torn tail is a
    live injected fault during a real `event_log.append` call, not a
    hand-crafted fixture."""
    event_log.append(home, "T-1", "Note", {"t": "1"}, actor="x", step_id="sid-1")
    pre = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    prior_step_ids = {"sid-1"}

    faults.inject_truncated_write(1)
    with pytest.raises(InjectedCrash):
        event_log.append(home, "T-1", "Note", {"t": "torn"}, actor="x", step_id="sid-2")

    # A truncated write never produced a parseable line -- the fault landed
    # before the operation could be considered to have happened at all.
    assert_crash_invariant(home, "T-1", pre=pre, post=pre, prior_step_ids=prior_step_ids)

    # The append that follows the crash must not be swallowed or corrupted.
    ev = event_log.append(home, "T-1", "Note", {"t": "3"}, actor="x", step_id="sid-3")
    assert ev is not None
    assert ev["payload"] == {"t": "3"}
    events = event_log.read(home, "T-1")
    assert [e["payload"] for e in events] == [{"t": "1"}, {"t": "3"}]

    # dedup still works across the torn tail
    dup = event_log.append(home, "T-1", "Note", {"t": "should-not-land"},
                           actor="x", step_id="sid-3")
    assert dup is None


def test_append_killed_after_write_before_fsync_lands_or_is_idempotently_retried(home, faults):
    """The write() syscall completed (a full, parseable line reached the
    page cache) before fsync raised -- unlike the truncation case, this
    crash lands *after* the event logically happened. A caller that sees the
    exception and retries with the same step_id must get a safe no-op, never
    a duplicate."""
    event_log.append(home, "T-1", "Note", {"t": "1"}, actor="x", step_id="sid-1")
    pre = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    prior_step_ids = {"sid-1"}
    post = snap_mod.fold("T-1", event_log.read(home, "T-1") +
                         [_expect_event(2, "Note", {"t": "2"}, "x", "sid-2")])

    faults.inject_fsync_raise(1)  # append_line's own, only, fsync call
    with pytest.raises(InjectedCrash):
        event_log.append(home, "T-1", "Note", {"t": "2"}, actor="x", step_id="sid-2")

    assert_crash_invariant(home, "T-1", pre=pre, post=post, prior_step_ids=prior_step_ids)

    # Blind retry with the same step_id: idempotent no-op if the write really
    # landed, a fresh append otherwise -- either way, exactly one sid-2 event.
    retry = event_log.append(home, "T-1", "Note", {"t": "2-retry"}, actor="x", step_id="sid-2")
    sid2 = [e for e in event_log.read(home, "T-1") if e["step_id"] == "sid-2"]
    assert len(sid2) == 1
    if retry is None:
        assert sid2[0]["payload"] == {"t": "2"}  # the crashed write actually landed


# ---------------------------------------------------------------------------
# ops.compact windows -- compact only relocates events with seq <= the
# snapshot's observed_seq, so a correct compact NEVER changes the fold: "pre"
# and "post" are always the same snapshot for every test in this section.
# ---------------------------------------------------------------------------

def _seed_compactable(home, key="T-1"):
    for i in range(1, 6):
        event_log.append(home, key, "Note", {"n": i}, actor="t")
    snap_mod.rebuild(home, key)                                    # observed_seq = 5
    event_log.append(home, key, "Note", {"n": 6}, actor="t", step_id="post-cutoff")
    return snap_mod.fold(key, event_log.read(home, key))


def test_compact_killed_during_archive_append(cfg, faults):
    home = cfg.home
    pre = _seed_compactable(home)
    prior_step_ids = {"post-cutoff"}

    faults.inject_fsync_raise(1)  # the archive append's own fsync
    with pytest.raises(InjectedCrash):
        ops.compact(cfg, "T-1")

    assert_crash_invariant(home, "T-1", pre=pre, post=pre, prior_step_ids=prior_step_ids)
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in active] == [1, 2, 3, 4, 5, 6]  # untouched


def test_compact_killed_between_archive_append_and_active_log_replace(cfg, faults):
    home = cfg.home
    pre = _seed_compactable(home)
    prior_step_ids = {"post-cutoff"}

    faults.inject_fsync_raise(2)  # archive fsync (#1) succeeds; atomic_write's
                                   # tmp-file fsync (#2), before os.replace, raises
    with pytest.raises(InjectedCrash):
        ops.compact(cfg, "T-1")

    assert_crash_invariant(home, "T-1", pre=pre, post=pre, prior_step_ids=prior_step_ids)
    # Archive holds the pre-cutoff events durably; the active log was never
    # replaced, so those events are (survivably) duplicated there too.
    archive = store.read_jsonl(store.events_archive_path(home, "T-1"))
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in archive] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in active] == [1, 2, 3, 4, 5, 6]


def test_compact_killed_after_replace_before_directory_fsync(cfg, faults):
    """The window RB-6's own tests don't cover: `os.replace` has *really*
    executed by this point -- the active log on disk already reflects the
    post-compact split -- and only the directory's own fsync (durability of
    the rename) fails."""
    home = cfg.home
    pre = _seed_compactable(home)
    prior_step_ids = {"post-cutoff"}

    faults.inject_fsync_raise(3)  # archive fsync (#1), tmp-file fsync (#2)
                                   # both succeed; the post-replace dir fsync (#3) raises
    with pytest.raises(InjectedCrash):
        ops.compact(cfg, "T-1")

    assert_crash_invariant(home, "T-1", pre=pre, post=pre, prior_step_ids=prior_step_ids)
    archive = store.read_jsonl(store.events_archive_path(home, "T-1"))
    active = store.read_jsonl(store.events_path(home, "T-1"))
    assert [e["seq"] for e in archive] == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in active] == [6]  # the replace really happened


def test_compact_flock_unavailable_fails_loudly_and_touches_nothing(cfg, faults):
    """The flock-unavailable window, exercised on `ops.compact` -- the other
    caller of `store.file_lock` besides `event_log.append` below."""
    home = cfg.home
    pre = _seed_compactable(home)
    active_before = store.events_path(home, "T-1").read_bytes()
    archive_before_exists = store.events_archive_path(home, "T-1").exists()

    faults.inject_flock_unavailable()
    with pytest.raises(InjectedCrash):
        ops.compact(cfg, "T-1")

    # Loud failure, not a silently-unserialized writer: nothing was touched.
    assert store.events_path(home, "T-1").read_bytes() == active_before
    assert store.events_archive_path(home, "T-1").exists() == archive_before_exists
    after = snap_mod.fold("T-1", event_log.read(home, "T-1"))
    assert _no_ts(after) == _no_ts(pre)


# ---------------------------------------------------------------------------
# flock unavailable, on the append path directly (the AC's own wording:
# "flock unavailable produces a loud failure, not a silently unserialised
# writer")
# ---------------------------------------------------------------------------

def test_append_flock_unavailable_fails_loudly_and_writes_nothing(home, faults):
    event_log.append(home, "T-1", "Note", {"t": "1"}, actor="x", step_id="sid-1")
    before = store.events_path(home, "T-1").read_bytes()

    faults.inject_flock_unavailable()
    with pytest.raises(InjectedCrash):
        event_log.append(home, "T-1", "Note", {"t": "2"}, actor="x", step_id="sid-2")

    # No unserialized write slipped through -- the file is byte-identical.
    assert store.events_path(home, "T-1").read_bytes() == before


# ---------------------------------------------------------------------------
# atomic_write windows, via the real snapshot.rebuild (its persisted cache
# file goes through store.write_json -> store.atomic_write). A crash here
# never touches the event log itself -- only whether the derived snapshot
# file is the pre- or post-rebuild content, never torn.
# ---------------------------------------------------------------------------

def test_atomic_write_killed_before_replace_leaves_the_old_snapshot_file(home, faults):
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")  # first snapshot write: observed_seq = 1
    before = store.read_json(store.snapshot_path(home, "T-1"))
    assert before["observed_seq"] == 1

    event_log.append(home, "T-1", "Note", {"n": 2}, actor="t")
    faults.inject_replace_raise(1)  # atomic_write's own os.replace
    with pytest.raises(InjectedCrash):
        snap_mod.rebuild(home, "T-1")

    # The event log itself is entirely unaffected by a snapshot-cache crash.
    events = event_log.read(home, "T-1")
    assert [e["seq"] for e in events] == [1, 2]
    # The derived cache still holds the pre-rebuild content -- parseable,
    # never torn.
    after = store.read_json(store.snapshot_path(home, "T-1"))
    assert after == before


def test_atomic_write_killed_between_replace_and_directory_fsync_lands_the_new_snapshot(home, faults):
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")
    event_log.append(home, "T-1", "Note", {"n": 2}, actor="t")

    faults.inject_fsync_raise(2)  # tmp-file fsync (#1) succeeds; the real
                                   # os.replace runs; the directory fsync (#2) raises
    with pytest.raises(InjectedCrash):
        snap_mod.rebuild(home, "T-1")

    # os.replace really executed -- the new content is what's on disk, still
    # fully parseable (never torn), even though the dir fsync never completed.
    after = store.read_json(store.snapshot_path(home, "T-1"))
    assert after["observed_seq"] == 2


# ---------------------------------------------------------------------------
# no-op fsync -- the shim's fifth supported fault kind: fsync claims success
# without truly flushing. Composed with a later real crash (os.replace
# raising), this proves atomic_write's crash-safety doesn't depend on fsync
# actually catching anything -- the ordering (write, fsync, replace, fsync
# dir) is what protects the pre-operation state, not fsync's return value.
# ---------------------------------------------------------------------------

def test_noop_fsync_does_not_mask_a_later_crash_before_replace(home, faults):
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    snap_mod.rebuild(home, "T-1")
    before = store.read_json(store.snapshot_path(home, "T-1"))
    event_log.append(home, "T-1", "Note", {"n": 2}, actor="t")

    faults.inject_fsync_noop(1)     # the tmp-file fsync silently "succeeds"
    faults.inject_replace_raise(1)  # ...then the replace really crashes

    with pytest.raises(InjectedCrash):
        snap_mod.rebuild(home, "T-1")

    after = store.read_json(store.snapshot_path(home, "T-1"))
    assert after == before  # still the pre-rebuild content -- never torn
