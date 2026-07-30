"""Snapshot = the folded projection of one ticket's event log.

Tiny (~1-2KB), disposable, machine-owned, DO-NOT-EDIT. The dispatcher reads only
snapshots to decide what is due, so a sweep never touches the full event history
or any of the old 100-500KB monoliths. Writers refresh the snapshot after every
append, so the dispatcher's cheap read is always current.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import events as E
from . import event_log, store
from .idempotency import content_hash
from .statemachine import Phase

# Spec acceptance criteria are Markdown task-list items: "- [ ] ..." / "- [x] ...".
_AC_RE = re.compile(r"^- \[[ xX]\]\s*(.+)$", re.MULTILINE)


def parse_acs(spec_text: str) -> list[str]:
    """Extract acceptance-criteria line texts (in spec order) from a spec's body."""
    return [m.group(1).strip() for m in _AC_RE.finditer(spec_text)]


def ac_hash(ac_text: str) -> str:
    """Content hash identifying one AC — invalidated by any edit to its line, so a
    human spec edit desyncs a stale attestation instead of mismatching by index."""
    return content_hash(ac_text.strip())


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
    failing_checks: list[str] = field(default_factory=list)
    unresolved_reviews: int = 0
    failure_count: int = 0
    last_error: str | None = None
    next_requeue_at: float | None = None
    open_questions: dict[str, str] = field(default_factory=dict)
    # qid → answer text for questions answered since the last phase change.
    # Survives crash-and-respawn so the reconciler can act on a folded answer
    # even when observed_seq has already advanced past the QuestionAnswered event.
    answered_questions: dict[str, str] = field(default_factory=dict)
    impl_turns: int = 0
    last_step: str | None = None
    kind: str = "implementation"
    proposal_path: str | None = None
    updated_ts: str | None = None
    # ac_hash -> evidence text, from AcVerified events. Never reset by a phase
    # change; only a spec edit that changes an AC's text invalidates an entry
    # (its hash simply stops matching any current AC — see acs_unverified()).
    ac_verified: dict[str, str] = field(default_factory=dict)
    # Set when a ticket originated from an external tracker (e.g. Jira) so the
    # dispatcher's sync tick knows which tickets to `refresh`.
    external_source: str | None = None
    external_id: str | None = None

    @property
    def question_open(self) -> bool:
        return bool(self.open_questions)

    def acs_unverified(self, spec_text: str) -> int:
        """Count ACs in *spec_text* with no matching AcVerified attestation.

        Matching is by content hash of the AC's own line, so editing an AC's text
        (even without adding/removing checkboxes) makes its old attestation stop
        counting — the human's edit desyncs it rather than silently keeping a
        now-stale "verified" against different wording.
        """
        hashes = {ac_hash(t) for t in parse_acs(spec_text)}
        return len(hashes - set(self.ac_verified.keys()))

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
            s.kind = p.get("kind", "implementation")
            s.external_source = p.get("external_source", s.external_source)
            s.external_id = p.get("external_id", s.external_id)
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
            s.answered_questions = {}
            s.unresolved_reviews = 0
        elif t == E.QUESTION_ASKED:
            s.open_questions[p.get("qid", str(seq))] = p.get("text", "")
        elif t == E.QUESTION_ANSWERED:
            qid = p.get("qid")
            s.open_questions.pop(qid, None)
            if qid:
                s.answered_questions[qid] = p.get("answer", "")
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
            s.failing_checks = p.get("failing_checks", [])
        elif t == E.REVIEW_FEEDBACK_RECEIVED:
            if p.get("state") == "CHANGES_REQUESTED":
                s.unresolved_reviews += 1
        elif t == E.IMPL_TURN:
            s.impl_turns = max(s.impl_turns, int(p.get("turn", s.impl_turns)))
        elif t == E.IMPL_STEP:
            s.impl_turns = max(s.impl_turns, int(p.get("turn", s.impl_turns)))
            if p.get("summary"):
                s.last_step = p["summary"]
        elif t == E.AC_VERIFIED:
            h = p.get("ac_hash")
            if h:
                s.ac_verified[h] = p.get("evidence", "")
        elif t == E.RESEARCH_PROPOSED:
            s.proposal_path = p.get("proposal_path", s.proposal_path)
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
    """Load the persisted snapshot, or a fresh TRIAGING default if none exists.

    Falls back to the archived location (``archive_done`` relocates a DONE
    ticket's snapshot there): otherwise a dependent whose ``dependsOn`` entry
    finished and got archived would see a phantom fresh-TRIAGING snapshot and
    block forever on a dependency that actually completed.
    """
    d = store.read_json(store.snapshot_path(home, key))
    if not d:
        d = store.read_json(store.archived_snapshot_path(home, key))
    if not d:
        return Snapshot(key=key)
    return Snapshot.from_dict(d)
