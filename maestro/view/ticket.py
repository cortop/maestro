"""Ticket-detail view-model -- header fields, phase dwell and the AC evidence matrix."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import event_log, ops, snapshot as snap_mod, store
from . import events as ev_view


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
        acs=ac_matrix(home, key, snap, spec))


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
