"""The two load-bearing correctness guarantees of the event log."""
import pytest

from maestro import event_log, store
from maestro.event_log import StaleAppendError


def test_seq_is_monotonic(home):
    for i in range(1, 6):
        ev = event_log.append(home, "T-1", "Note", {"n": i}, actor="t")
        assert ev["seq"] == i
    assert event_log.last_seq(home, "T-1") == 5


def test_step_id_dedup_is_idempotent(home):
    """Two racing reconcilers computing the SAME action -> exactly one event."""
    first = event_log.append(home, "T-1", "PrOpened", {"number": 42},
                             actor="a", step_id="open-pr-T-1")
    second = event_log.append(home, "T-1", "PrOpened", {"number": 99},
                              actor="b", step_id="open-pr-T-1")
    assert first is not None
    assert second is None  # idempotent no-op
    prs = [e for e in event_log.read(home, "T-1") if e["type"] == "PrOpened"]
    assert len(prs) == 1
    assert prs[0]["payload"]["number"] == 42  # the first writer won


def test_fencing_rejects_stale_append(home):
    """A worker that folded an older tail loses the optimistic race."""
    event_log.append(home, "T-1", "Note", {"n": 1}, actor="a")  # seq 1
    # Worker A folded at seq 1 and intends to append "believing tail == 1".
    # Meanwhile worker B appends, moving the tail to 2.
    event_log.append(home, "T-1", "Note", {"n": 2}, actor="b")  # seq 2
    with pytest.raises(StaleAppendError):
        event_log.append(home, "T-1", "Note", {"n": 3}, actor="a", expected_last_seq=1)
    # The CAS append with the CORRECT expected tail succeeds.
    ev = event_log.append(home, "T-1", "Note", {"n": 3}, actor="a", expected_last_seq=2)
    assert ev["seq"] == 3


def test_step_id_dedup_shadows_a_stale_fencing_check(home):
    """Ordering (RB-7): dedup is checked BEFORE fencing. A caller replaying an
    already-applied step_id gets the idempotent `None` no-op even if its
    `expected_last_seq` is now stale -- it never reaches the CAS at all."""
    first = event_log.append(home, "T-1", "PhaseChanged", {"phase": "ready"},
                             actor="a", step_id="phase-T-1-ready", expected_last_seq=0)
    assert first is not None and first["seq"] == 1
    event_log.append(home, "T-1", "Note", {}, actor="b")  # tail moves to 2

    # Same step_id, still asserting the now-stale expected_last_seq=0 -- dedup
    # wins and this returns None, NOT StaleAppendError.
    replay = event_log.append(home, "T-1", "PhaseChanged", {"phase": "ready"},
                              actor="a", step_id="phase-T-1-ready", expected_last_seq=0)
    assert replay is None

    # A genuinely NEW step_id with the same stale expected_last_seq is not
    # shadowed by dedup, so the CAS actually runs and rejects it.
    with pytest.raises(StaleAppendError):
        event_log.append(home, "T-1", "PhaseChanged", {"phase": "implementing"},
                         actor="a", step_id="phase-T-1-implementing", expected_last_seq=0)


def test_keys_are_isolated(home):
    event_log.append(home, "T-1", "Note", {}, actor="t")
    event_log.append(home, "T-2", "Note", {}, actor="t")
    assert event_log.last_seq(home, "T-1") == 1
    assert event_log.last_seq(home, "T-2") == 1


def test_read_since(home):
    for i in range(5):
        event_log.append(home, "T-1", "Note", {"n": i}, actor="t")
    later = event_log.read(home, "T-1", since=3)
    assert [e["seq"] for e in later] == [4, 5]


def test_read_dedups_seq_duplicated_across_archive_and_active(home):
    """RB-2 (c): an interrupted `ops.compact` (archive-append succeeded, the
    active-log rewrite that would have dropped the archived tail didn't) can
    leave the same seq in both files -- `read` must return it exactly once,
    keeping the archive's copy (read first)."""
    from maestro import store
    import json

    ev1 = event_log.append(home, "T-1", "Note", {"n": 1}, actor="t")
    ev2 = event_log.append(home, "T-1", "Note", {"n": 2}, actor="t")
    archive_path = store.events_archive_path(home, "T-1")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev1, separators=(",", ":")) + "\n")  # duplicated into the archive
    events = event_log.read(home, "T-1")
    assert [e["seq"] for e in events] == [1, 2]  # not [1, 1, 2]
    assert events[0]["payload"] == ev1["payload"]  # the archive's copy, not dropped either


def test_torn_tail_does_not_swallow_the_next_append(home):
    """A crashed writer that leaves an unterminated line (RB-1's repro) must not
    corrupt or swallow the append that follows it, and must not disarm step-id
    dedup for that swallowed append's own step-id.

    RB-11 note: this fixes the torn tail in place as a static precondition.
    `tests/test_crash_injection.py::test_append_killed_mid_line_...` proves the
    same claim generated live, via an actual injected mid-write crash during a
    real `event_log.append` call -- kept separately since the two exercise
    different things (recovery-from-artifact vs. crash-during-the-call)."""
    event_log.append(home, "T-1", "Note", {"t": "1"}, actor="x", step_id="sid-1")
    path = store.events_path(home, "T-1")
    with path.open("a") as fh:
        # a writer that died mid-line -- no trailing newline
        fh.write('{"seq": 2, "type": "Note", "payload": {"t": "torn"')

    seqs_before = [e["seq"] for e in event_log.read(home, "T-1")]  # the torn line is unreadable
    ev = event_log.append(home, "T-1", "Note", {"t": "3"}, actor="x", step_id="sid-3")
    assert ev is not None  # no longer swallowed
    assert ev["payload"] == {"t": "3"}

    events = event_log.read(home, "T-1")
    assert [e["payload"] for e in events] == [{"t": "1"}, {"t": "3"}]
    # no seq is reused: the new event's seq is strictly greater than every seq
    # that was readable from the log before it (the torn line contributed none,
    # being unparseable)
    assert ev["seq"] > max(seqs_before)

    # dedup still works across the torn tail: replaying sid-3 is an idempotent no-op
    dup = event_log.append(home, "T-1", "Note", {"t": "should-not-land"},
                           actor="x", step_id="sid-3")
    assert dup is None
    sid3_events = [e for e in event_log.read(home, "T-1") if e["step_id"] == "sid-3"]
    assert len(sid3_events) == 1
    assert sid3_events[0]["payload"] == {"t": "3"}
