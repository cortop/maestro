"""Deterministic step ids — the idempotency crux.

A step id is derived ONLY from log content (phase + observed sequence + action),
never from wall-clock or run id. So two reconcilers that race on the same frozen
log compute the *same* step id for the same next action; the event log's step-id
de-dup then collapses them to a single recorded effect (one PrOpened, not two).

This is what makes crash-and-respawn safe: a worker that died after writing its
event but before exiting is re-spawned, recomputes the identical step id, sees it
already present, and skips the external side effect.
"""
from __future__ import annotations

import hashlib


def step_id(key: str, phase: str, observed_seq: int, action: str) -> str:
    h = hashlib.sha256()
    h.update(f"{key}\x1f{phase}\x1f{observed_seq}\x1f{action}".encode("utf-8"))
    return h.hexdigest()[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
