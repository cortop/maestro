"""The two load-bearing correctness guarantees of the event log."""
import pytest

from maestro import event_log
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
