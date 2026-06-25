"""Snapshot = the folded projection of one ticket's event log.

Tiny (~1-2KB), disposable, machine-owned, DO-NOT-EDIT. The dispatcher reads only
snapshots to decide what is due, so a sweep never touches the full event history
or any of the old 100-500KB monoliths. Writers refresh the snapshot after every
append, so the dispatcher's cheap read is always current.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import events as E
from . import event_log, store
from .statemachine import Phase


@dataclass
class Snapshot:
    key: str
    phase: str = Phase.TRIAGING.value
    observed_seq: int = 0
    spec_hash: str | None = None
    tier: str | None = None
    title: str | None = None
    source: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_draft: bool | None = None
    ci_state: str | None = None
    failure_count: int = 0
    last_error: str | None = None
    next_requeue_at: float | None = None
    open_questions: dict[str, str] = field(default_factory=dict)
    impl_turns: int = 0
    last_step: str | None = None
    updated_ts: str | None = None

    @property
    def question_open(self) -> bool:
        return bool(self.open_questions)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["question_open"] = self.question_open
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def fold(key: str, events: list[dict]) -> Snapshot:
    """Replay events into a Snapshot. Pure function of the log — the whole point."""
    s = Snapshot(key=key)
    for ev in events:
        seq = ev.get("seq")
        if isinstance(seq, int):
            s.observed_seq = seq
        t = ev.get("type")
        p = ev.get("payload") or {}
        s.updated_ts = ev.get("ts", s.updated_ts)

        if t == E.TICKET_CREATED:
            s.title = p.get("title", s.title)
            s.source = p.get("source", s.source)
            s.spec_hash = p.get("spec_hash", s.spec_hash)
            s.phase = Phase.TRIAGING.value
        elif t == E.SPEC_OBSERVED:
            s.spec_hash = p.get("spec_hash", s.spec_hash)
        elif t == E.TICKET_TRIAGED:
            s.tier = p.get("tier", s.tier)
            if p.get("phase"):
                s.phase = Phase(p["phase"]).value
        elif t == E.PHASE_CHANGED:
            s.phase = Phase(p["phase"]).value
            s.failure_count = 0
            s.next_requeue_at = None
        elif t == E.QUESTION_ASKED:
            s.open_questions[p.get("qid", str(seq))] = p.get("text", "")
        elif t == E.QUESTION_ANSWERED:
            s.open_questions.pop(p.get("qid"), None)
        elif t == E.PR_OPENED:
            s.pr_number = p.get("number", s.pr_number)
            s.pr_url = p.get("url", s.pr_url)
            s.pr_draft = p.get("draft", True)
            s.pr_state = "open"
        elif t == E.PR_UPDATED:
            if p.get("merged"):
                s.pr_state = "merged"
            if "draft" in p:
                s.pr_draft = p["draft"]
        elif t == E.CI_OBSERVED:
            s.ci_state = p.get("state", s.ci_state)
        elif t == E.IMPL_TURN:
            s.impl_turns = max(s.impl_turns, int(p.get("turn", s.impl_turns)))
        elif t == E.IMPL_STEP:
            s.impl_turns = max(s.impl_turns, int(p.get("turn", s.impl_turns)))
            if p.get("summary"):
                s.last_step = p["summary"]
        elif t == E.REQUEUE_SCHEDULED:
            s.next_requeue_at = p.get("at")
        elif t == E.FAILED:
            s.failure_count += 1
            s.last_error = p.get("error", s.last_error)
        elif t == E.STALLED:
            s.phase = Phase.DEGRADED.value
            s.last_error = p.get("reason", s.last_error)
        elif t == E.FINALIZED:
            s.phase = Phase.DONE.value
            s.next_requeue_at = None
    return s


def rebuild(home: Path, key: str) -> Snapshot:
    """Fold the full log and atomically persist the snapshot."""
    snap = fold(key, event_log.read(home, key))
    store.write_json(store.snapshot_path(home, key), snap.to_dict())
    return snap


def load(home: Path, key: str) -> Snapshot:
    """Load the persisted snapshot, or a fresh TRIAGING default if none exists."""
    d = store.read_json(store.snapshot_path(home, key))
    if not d:
        return Snapshot(key=key)
    return Snapshot.from_dict(d)
