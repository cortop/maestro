"""Logs view-model: a ticket's sessions split into the live one (full detail) and past ones (summary rows)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import claims, event_log, steplog
from ..sessions import list_pruned, list_sessions
from . import rounds as rounds_view


@dataclass(frozen=True)
class PastSession:
    session_id: str
    summary: str          # one-line digest: start, runner format, outcome, model, calls
    sess: dict            # the ``list_sessions`` / ``list_pruned`` entry
    pruned: bool = False  # raw log gone; ``summary`` comes from the persisted sidecar


@dataclass(frozen=True)
class TicketLogs:
    key: str
    live: dict | None                 # the session shown with full step detail, None when none exist
    past: tuple[PastSession, ...]     # newest first; empty means no summary section at all


def live_path(home: Path, key: str, sessions: list[dict]) -> str | None:
    """The session log being tailed: the claim's log, else the newest captured session."""
    claim = claims.read_claim(home, key)
    if claim and claim.get("log_path"):
        return str(claim["log_path"])
    return sessions[0]["path"] if sessions else None


def build(home: Path, key: str) -> TicketLogs:
    """Every session of *key* with the live one separated from collapsed past summaries."""
    listed = list_sessions(home, key, with_outcome=True)
    live = live_path(home, key, listed)
    events = event_log.read(home, key) if (listed or list_pruned(home, key)) else []
    newest = listed[0]["path"] if listed else None
    entries = [*listed, *list_pruned(home, key)]
    entries.sort(key=lambda s: s.get("epoch", 0.0), reverse=True)
    live_sess, past = None, []
    for s in entries:
        if not s.get("pruned") and s["path"] == live:
            live_sess = s
            continue
        d = rounds_view.session_digest(home, key, s, events, newest)
        past.append(PastSession(s["session_id"], steplog.format_session_oneliner(s, d), s,
                                bool(s.get("pruned"))))
    return TicketLogs(key, live_sess, tuple(past))


def detail_lines(home: Path, key: str, past: PastSession) -> list[str]:
    """The expanded view of a past session: its step digest lines."""
    d = rounds_view.session_digest(home, key, past.sess)
    return steplog.format_session_digest(d)
