"""The append-only, fencing-gated event log — the heart of the system.

Append rules, enforced under an exclusive per-key lock:

1. **step-id idempotency** — if an event with the same ``step_id`` already exists,
   the append is a successful no-op (the action already happened). This makes
   crash-and-respawn and two-racing-reconcilers safe.
2. **optimistic concurrency (fencing)** — a caller that folded the log up to
   ``expected_last_seq`` is asserting "nobody has appended since I looked." If the
   real tail moved on, the append is rejected with ``StaleAppendError`` and the
   caller bails; the dispatcher re-derives the ticket next sweep.
3. **single writer** — the lock guarantees one appender at a time per key; distinct
   keys never contend.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import store


class StaleAppendError(store.MaestroError):
    """The log advanced since the caller folded it (lost the optimistic race)."""


def _scan_file(path: Path, last_seq: int, step_ids: set[str]) -> tuple[int, set[str]]:
    if not path.exists():
        return last_seq, step_ids
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            seq = ev.get("seq")
            if isinstance(seq, int) and seq > last_seq:
                last_seq = seq
            sid = ev.get("step_id")
            if sid:
                step_ids.add(sid)
    return last_seq, step_ids


def _scan_tail(path: Path) -> tuple[int, set[str]]:
    """Return (last_seq, step_ids_seen) across active log and its archive."""
    archive = path.parent / (path.stem + ".archive.jsonl")
    last_seq, step_ids = _scan_file(archive, 0, set())
    return _scan_file(path, last_seq, step_ids)


def last_seq(home: Path, key: str) -> int:
    seq, _ = _scan_tail(store.events_path(home, key))
    return seq


def append(
    home: Path,
    key: str,
    type: str,
    payload: dict | None = None,
    *,
    actor: str,
    step_id: str | None = None,
    expected_last_seq: int | None = None,
) -> dict | None:
    """Append one event. Returns the written event, or ``None`` if it was a
    step-id-idempotent no-op. Raises ``StaleAppendError`` on a lost fencing race.

    **Ordering: step-id dedup is checked BEFORE fencing** (below: the ``step_id``
    branch precedes the ``expected_last_seq`` branch). This means a ``None``
    return tells you only "this exact step already happened" -- it does NOT
    tell you whether ``expected_last_seq`` was fresh or stale, because a caller
    that recomputes the same deterministic ``step_id`` from the state it last
    knew about (the ordinary crash-and-respawn replay) is short-circuited to a
    clean no-op before the tail is even compared. That is intentional and
    load-bearing: replaying an already-applied step must always succeed
    idempotently, even if the log has since moved on for OTHER reasons, or
    crash-and-respawn would spuriously raise instead of no-opping. A caller
    that wants the fencing check to actually run must pass a ``step_id`` that
    is either absent or genuinely novel for a stale fold -- see
    ``ops.set_phase``, where the phase-transition ``step_id`` is derived from
    the caller's ``(phase, observed_seq)``, so a stale caller's step_id differs
    from the fresh one and dedup does not shadow the CAS.
    """
    store.validate_key(key)
    path = store.events_path(home, key)
    with store.file_lock(path):
        tail_seq, step_ids = _scan_tail(path)
        if step_id is not None and step_id in step_ids:
            return None  # already applied — idempotent success
        if expected_last_seq is not None and tail_seq != expected_last_seq:
            raise StaleAppendError(
                f"{key}: expected tail {expected_last_seq}, found {tail_seq}"
            )
        seq = tail_seq + 1
        event = {
            "seq": seq,
            "ts": store.iso_now(),
            "key": key,
            "actor": actor,
            "type": type,
            "step_id": step_id,
            "fencing_token": seq,
            "payload": payload or {},
        }
        store.append_line(path, json.dumps(event, separators=(",", ":")))
        return event


def read(home: Path, key: str, *, since: int = 0) -> list[dict]:
    """All events for a key with seq > ``since`` (oldest first), archive included.

    Deduplicates by seq (RB-6 introduced this dedup for the compaction crash
    window; RB-2 law (c) requires it hold independent of that specific trigger).
    ``ops.compact`` fsyncs its archive append before it replaces the active log
    (see there), so a crash in between can leave the same events present in both
    files -- byte-identical, since compaction never rewrites an event, only
    relocates it. Keeping the first occurrence (the archive copy) and dropping
    the rest is what makes that window survivable: without it, a duplicated
    event would replay twice through ``fold`` and corrupt state that isn't
    naturally idempotent (e.g. ``failure_count``).
    """
    events: list[dict] = []
    events += store.read_jsonl(store.events_archive_path(home, key))
    events += store.read_jsonl(store.events_path(home, key))
    events = [e for e in events if isinstance(e.get("seq"), int) and e["seq"] > since]
    seen: set[int] = set()
    deduped: list[dict] = []
    for e in events:
        seq = e["seq"]
        if seq in seen:
            continue
        seen.add(seq)
        deduped.append(e)
    deduped.sort(key=lambda e: e["seq"])
    return deduped
