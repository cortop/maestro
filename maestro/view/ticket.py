"""Ticket-detail view-model -- header fields, phase dwell and the AC evidence matrix."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import event_log, ops, snapshot as snap_mod, store
from ..config import Config
from . import events as ev_view
from . import rounds as rounds_view
from . import why as why_view

# Pipeline order of the phase rail; a phase the ticket never visited still gets an entry.
RAIL_ORDER = ("triaging", "researching", "awaiting-human", "ready", "implementing", "verifying",
              "qa", "awaiting-ci", "in-review", "terminating", "done")
# Phases whose bounce back to implementing means a CI / QA / test failure sent it back.
_FAIL_SOURCES = frozenset({"qa", "awaiting-ci", "in-review", "verifying"})
# A review ticket never builds: its rail is the inbound-review pipeline, in its own words.
REVIEW_RAIL = (("ready", "queued"), ("verifying", "tests"), ("qa", "checking"),
               ("awaiting-human", "brief ready"), ("done", "submitted"))


@dataclass(frozen=True)
class RailEntry:
    phase: str
    dwell_s: float
    visits: int
    current: bool
    failed: bool
    label: str = ""

    def __post_init__(self):
        if not self.label:
            object.__setattr__(self, "label", self.phase)


@dataclass
class TicketView:
    key: str
    title: str
    phase: str
    snap: snap_mod.Snapshot
    spec: str
    proposal: str | None
    dwell: list[ev_view.DwellSegment]
    acs: list[dict]
    status: str = ""
    rail: list[RailEntry] | None = None


def exists(home: Path, key: str) -> bool:
    return store.spec_path(home, key).exists() or store.events_path(home, key).exists()


def ac_matrix(home: Path, key: str, snap: snap_mod.Snapshot | None = None,
              spec: str | None = None, tree_key: str | None = None) -> list[dict]:
    """One row per spec AC with self-attestation, QA verdicts and the latest check
    (`ops.ac_evidence_rows`); *tree_key* ``None`` means no check is current."""
    snap = snap or snap_mod.load(home, key)
    if spec is None:
        spec = read_text(store.spec_path(home, key))
    return ops.ac_evidence_rows(snap, spec, tree_key)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def ticket(home: Path, key: str, now: float) -> TicketView:
    """Everything the detail page shows; raises `store.MaestroError`/OSError never for a
    missing ticket -- callers gate on `exists`."""
    snap = snap_mod.load(home, key)
    spec = read_text(store.spec_path(home, key))
    proposal_path = store.ticket_dir(home, key) / "proposal.md"
    proposal = read_text(proposal_path) if proposal_path.exists() else None
    return TicketView(
        key=key, title=snap_mod.display_title(home, snap), phase=snap.phase, snap=snap,
        spec=spec, proposal=proposal,
        dwell=ev_view.phase_dwell(event_log.read(home, key), now),
        acs=ac_matrix(home, key, snap, spec),
        status=status_sentence(home, key, snap),
        rail=phase_rail(event_log.read(home, key), now, snap.kind))


def phase_rail(events: list[dict], now: float, kind: str = "implementation") -> list[RailEntry]:
    """One entry per phase in pipeline order: total dwell, visit count, a ``current`` marker
    and a ``failed`` marker for a phase a CI/QA/test failure sent back to implementing.
    Phases outside ``RAIL_ORDER`` (e.g. degraded) follow, in first-visit order. A
    ``review`` ticket uses ``REVIEW_RAIL`` and its labels instead."""
    segs = ev_view.phase_dwell(events, now)
    dwell: dict[str, float] = {}
    visits: dict[str, int] = {}
    failed: set[str] = set()
    for i, seg in enumerate(segs):
        dwell[seg.phase] = dwell.get(seg.phase, 0.0) + seg.seconds
        visits[seg.phase] = visits.get(seg.phase, 0) + 1
        nxt = segs[i + 1].phase if i + 1 < len(segs) else None
        if seg.phase in _FAIL_SOURCES and nxt == "implementing":
            failed.add(seg.phase)
    current = segs[-1].phase if segs else None
    labels = dict(REVIEW_RAIL) if kind == "review" else {}
    order = tuple(labels) if labels else RAIL_ORDER
    extra = [p for p in visits if p not in order]
    return [RailEntry(p, dwell.get(p, 0.0), visits.get(p, 0), p == current, p in failed,
                      labels.get(p, ""))
            for p in (*order, *extra)]


def status_sentence(home: Path, key: str, snap: snap_mod.Snapshot) -> str:
    """One line: who is acting and on what, from the phase, the round label and the snapshot."""
    phase = snap.phase
    label = rounds_view.round_label(home, key)
    if snap.kind == "review":
        return _review_status(snap)
    if phase == "done":
        return "Done."
    if phase == "degraded":
        return "Parked by the dispatcher" + (f": {snap.last_error}" if snap.last_error else ".")
    if phase == "awaiting-human" and (snap.open_questions or snap.question_open):
        return "Waiting on you to answer."
    if phase == "awaiting-ci":
        if snap.ci_state == "failure" or snap.failing_checks:
            return "The dispatcher is handing a CI failure back to an agent."
        return "The dispatcher is waiting on CI."
    if phase == "in-review":
        return "Waiting on you to review and merge."
    if phase == "verifying":
        return "The dispatcher is running the test suite."
    if phase == "terminating":
        return "The dispatcher is running teardown."
    if label:
        return f"An agent is working: {label}."
    return f"In {phase}."


def _review_status(snap: snap_mod.Snapshot) -> str:
    """The status sentence of a `review` ticket: checking, brief ready, or waiting on the submit."""
    if snap.phase == "done":
        return "Review finished."
    if snap.phase == "degraded":
        return "Parked by the dispatcher" + (f": {snap.last_error}" if snap.last_error else ".")
    if snap.phase == "awaiting-human":
        if snap.review_brief:
            return "Brief ready: edit the comments, then waiting on your submit."
        return "Waiting on you to answer."
    if snap.phase == "verifying":
        return "The dispatcher is running the PR's tests, then an agent checks it."
    return "An agent is checking the pull request."


def dispatch_card(home: Path, key: str, snap: snap_mod.Snapshot, cfg: Config, now: float) -> dict:
    """The dispatch verdict as a card: verdict, claim, attempts, throttle and open deps,
    each text built by `view/why`. Reads only (never releases a claim)."""
    res, verdict = why_view.now_verdict(home, key, snap, now)
    ctx = why_view.gather_context(home, key, cfg, now)
    return {
        "due": res.due,
        "reason": res.reason,
        "verdict": verdict,
        "claim": why_view.claim_text(ctx),
        "attempts": why_view.attempts_text(snap, cfg, ctx),
        "throttle": why_view.throttle_text(home, key, snap, res, cfg, ctx, now),
        "open_deps": [{"key": d, "phase": p} for d, p in why_view.open_deps(home, key)],
    }


EVENTS_PAGE = 50


@dataclass
class EventPage:
    events: list[dict]
    next_before: int | None


def event_page(home: Path, key: str, before: int | None = None, limit: int = EVENTS_PAGE) -> EventPage:
    """Newest-first page of *key*'s events with ``seq < before``; ``next_before`` is the
    cursor for the following (older) page, or ``None`` at the start of the log. Paging by
    seq keeps pages gapless and duplicate-free while new events append."""
    evs = event_log.read(home, key)
    if before is not None:
        evs = [e for e in evs if e["seq"] < before]
    page = evs[-limit:][::-1]
    nxt = page[-1]["seq"] if page and len(evs) > limit else None
    return EventPage(events=page, next_before=nxt)
