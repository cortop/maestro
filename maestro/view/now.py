"""Now view-model -- the home screen's needs-you, in-flight and queued groups -- no UI dependency."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import depgraph, event_log, fleet as fleet_mod, inbox, ops, projection, snapshot as snap_mod, store
from .. import events as E
from ..config import Config
from ..dispatcher import DISPATCHER_OWNED_PHASES
from ..statemachine import TERMINAL_PHASES, Phase
from . import board as board_view
from . import rounds as rounds_view
from . import why as why_view

NEEDS_YOU_PHASES = board_view.NEEDS_YOU_PHASE_VALUES
_DISPATCHER_PHASES = frozenset(p.value for p in DISPATCHER_OWNED_PHASES) | {Phase.TERMINATING.value}


@dataclass(frozen=True)
class QuestionCard:
    key: str
    title: str
    qid: str
    body: str
    recommendation: str | None
    position: int | None
    total: int | None
    waiting_s: float
    dependents: tuple[str, ...]


@dataclass(frozen=True)
class DegradedCard:
    key: str
    title: str
    reason: str
    waiting_s: float
    dependents: tuple[str, ...]


@dataclass(frozen=True)
class InFlightRow:
    key: str
    title: str
    phase: str
    owner: str          # "agent" | "dispatcher"
    round_label: str
    doing: str
    tool_line: str


@dataclass(frozen=True)
class QueuedRow:
    key: str
    title: str
    phase: str
    reason: str


@dataclass
class NowView:
    needs_you: list[QuestionCard | DegradedCard] = field(default_factory=list)
    in_flight: list[InFlightRow] = field(default_factory=list)
    queued: list[QueuedRow] = field(default_factory=list)


def _epoch(ts: str | None, default: float) -> float:
    value = store.iso_to_epoch(ts)
    return default if value is None else value


def _ask_times(home: Path, key: str) -> dict[str, float]:
    """qid -> epoch of its latest QuestionAsked event."""
    out: dict[str, float] = {}
    for ev in event_log.read(home, key):
        if ev["type"] == E.QUESTION_ASKED:
            qid = ev.get("payload", {}).get("qid", str(ev["seq"]))
            ts = store.iso_to_epoch(ev.get("ts"))
            if ts is not None:
                out[qid] = ts
    return out


def _queued_for(home: Path, key: str) -> tuple[set[str], bool]:
    """(qids with a queued answer, whether an untargeted/ticket-level command is queued)."""
    qids: set[str] = set()
    whole = False
    for cmd in inbox.pending(home, key):
        name = cmd.get("command", "")
        if name in ("retry", "discard"):
            whole = True
        elif name in ops.ANSWER_COMMANDS:
            qid = (cmd.get("args") or {}).get("qid")
            if qid:
                qids.add(qid)
            else:
                whole = True
    return qids, whole


def _cards(home: Path, snap: snap_mod.Snapshot, graph: depgraph.DepGraph, now: float) -> list:
    title = snap_mod.display_title(home, snap)
    dependents = tuple(graph.dependents.get(snap.key, ()))
    since = _epoch(snap.updated_ts, now)
    queued_qids, queued_whole = _queued_for(home, snap.key)  # answered, not yet folded
    if queued_whole:
        return []
    if snap.phase == Phase.DEGRADED.value:
        reason = snap.last_error or "degraded"
        return [DegradedCard(snap.key, title, reason, max(0.0, now - since), dependents)]
    asked = _ask_times(home, snap.key)
    cards = []
    for qid, text in snap.open_questions.items():
        if qid in queued_qids:
            continue
        position, total, body, rec = ops.parse_round_question(text)
        cards.append(QuestionCard(snap.key, title, qid, body, rec, position, total,
                                  max(0.0, now - asked.get(qid, since)), dependents))
    if not cards and not queued_qids:  # awaiting-human with no open question yet
        cards.append(DegradedCard(snap.key, title, snap.last_error or "awaiting your input",
                                  max(0.0, now - since), dependents))
    return cards


def _queued_reason(home: Path, snap: snap_mod.Snapshot, now: float) -> str:
    hold = fleet_mod.hold_state(home, snap.key, now)
    if hold is not None:
        until = hold.get("until")
        if until is not None:
            try:
                return "held until " + datetime.fromtimestamp(float(until)).strftime("%H:%M")
            except (TypeError, ValueError):
                pass
        return "held" + (f": {hold['reason']}" if hold.get("reason") else "")
    return why_view.now_verdict(home, snap.key, snap, now)[1]


def build(home: Path, cfg: Config, now: float | None = None) -> NowView:
    """Everything the Now screen shows. Blocking (claims probe, log tail): off the UI thread."""
    now = store.now_epoch() if now is None else now
    snaps = [s for s in projection.load_snapshots(home) if Phase(s.phase) not in TERMINAL_PHASES]
    graph = depgraph.build(home)
    running = board_view.running_keys(home, cfg)
    live = board_view.live_statuses(home, running)
    view = NowView()
    for snap in snaps:
        key = snap.key
        title = snap_mod.display_title(home, snap)
        if snap.phase in NEEDS_YOU_PHASES:
            view.needs_you.extend(_cards(home, snap, graph, now))
        elif key in running or snap.phase in _DISPATCHER_PHASES:
            doing, tool = board_view.live_lines(live.get(key), now)
            owner = "dispatcher" if snap.phase in _DISPATCHER_PHASES and key not in running else "agent"
            view.in_flight.append(InFlightRow(key, title, snap.phase, owner,
                                              rounds_view.round_label(home, key), doing, tool))
        else:
            view.queued.append(QueuedRow(key, title, snap.phase, _queued_reason(home, snap, now)))
    return view
