"""Event-log view-models -- dwell, category, summary and the board-wide tail; stdlib only."""
from __future__ import annotations

import json as _json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import event_log, events as ev_types, store
from ..statemachine import Phase

_IMPL_STEP = "ImplStepRecorded"


@dataclass(frozen=True)
class DwellSegment:
    phase: str
    seconds: float
    forced: bool = False
    current: bool = False


def _phase_moves(events: list[dict]) -> list[dict]:
    """The phase-moving events as ``{phase, ts, forced, seq, reason}`` moves (oldest first).

    Mirrors the fold arms in ``snapshot``: TicketCreated -> triaging, PhaseChanged
    (valid ``phase``) -> that phase, Stalled -> degraded, Finalized -> done. An unknown
    phase and a move to the phase already current are no-ops, and ``done`` absorbs.
    """
    moves: list[dict] = []
    for ev in events:
        t = ev.get("type")
        payload = ev.get("payload") or {}
        forced = False
        reason = ""
        if t == "TicketCreated":
            phase = Phase.TRIAGING.value
        elif t == "PhaseChanged":
            try:
                phase = Phase(payload.get("phase")).value
            except ValueError:
                continue
            forced = bool(payload.get("forced_by"))
            reason = payload.get("reason") or ""
        elif t == "Stalled":
            phase = Phase.DEGRADED.value
            reason = payload.get("reason") or ""
        elif t == "Finalized":
            phase = Phase.DONE.value
        else:
            continue
        ts = store.iso_to_epoch(ev.get("ts"))
        if ts is None:
            continue
        if moves and (moves[-1]["phase"] == Phase.DONE.value or moves[-1]["phase"] == phase):
            continue
        moves.append({"phase": phase, "ts": ts, "forced": forced,
                      "seq": ev.get("seq"), "reason": reason})
    return moves


def phase_dwell(events: list[dict], now: float) -> list[DwellSegment]:
    """Fold the phase moves into per-phase dwell segments (oldest first).

    The last segment is ``current`` and runs up to *now*.
    """
    moves = _phase_moves(events)
    segs: list[DwellSegment] = []
    for i, m in enumerate(moves):
        last = i == len(moves) - 1
        end = now if last else moves[i + 1]["ts"]
        segs.append(DwellSegment(m["phase"], max(0.0, end - m["ts"]), m["forced"], last))
    return segs


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


# --- board-wide activity ticker (T-173) -------------------------------------

CATEGORY_NAMES = {1: "lifecycle", 2: "human", 3: "vcs/ci", 4: "evidence",
                  5: "work", 6: "other"}
_OTHER_CATEGORY = 6

CATEGORY_OF = {
    **dict.fromkeys((ev_types.TICKET_CREATED, ev_types.SPEC_OBSERVED, ev_types.PHASE_CHANGED,
                     ev_types.FINALIZED, ev_types.FAILED, ev_types.STALLED), 1),
    **dict.fromkeys((ev_types.QUESTION_ASKED, ev_types.QUESTION_ANSWERED,
                     ev_types.COMMAND_RECEIVED, ev_types.FAST_PATH_DECIDED,
                     ev_types.ALLOWED_PATHS_ADDED,
                     ev_types.APPROVED), 2),
    **dict.fromkeys((ev_types.PR_OPENED, ev_types.PR_UPDATED, ev_types.CI_OBSERVED,
                     ev_types.CI_RERUN_REQUESTED, ev_types.REVIEW_FEEDBACK_RECEIVED,
                     ev_types.REVIEW_REPLY_POSTED, ev_types.REVIEW_ROUND_STARTED, ev_types.REVIEW_FINDING_RECORDED,
                     ev_types.REVIEW_BRIEF_WRITTEN, ev_types.RESTACK_QUEUED,
                     ev_types.RESTACK_COMPLETED), 3),
    **dict.fromkeys((ev_types.AC_VERIFIED, ev_types.TEST_RUN_CAPTURED,
                     ev_types.AC_CHECK_CAPTURED, ev_types.AC_QA_VERDICT,
                     ev_types.POST_QA_SKILL_SPAWNED, ev_types.RESEARCH_PROPOSED), 4),
    **dict.fromkeys((ev_types.IMPL_TURN, ev_types.IMPL_STEP, ev_types.NOTE), 5),
    **dict.fromkeys((ev_types.REQUEUE_SCHEDULED, ev_types.CHECKED, ev_types.JIRA_SYNCED,
                     ev_types.LINEAR_SYNCED, ev_types.LINEAR_STATUS_PUSHED), 6),
}


def category_of(type_: str) -> int:
    """Activity group (1-6) of an event type; an unknown type is group 6."""
    return CATEGORY_OF.get(type_, _OTHER_CATEGORY)


_SUMMARY_CAP = 160

# Event type -> payload keys tried in order; the first non-empty value is the summary.
_LEAD_KEYS = {
    ev_types.REVIEW_REPLY_POSTED: ("body",),
    ev_types.NOTE: ("text", "note", "summary", "body"),
    ev_types.POST_QA_SKILL_SPAWNED: ("skill",),
}


def _first_line(value: object) -> str:
    """The first non-empty line of ``str(value)``, whitespace-trimmed."""
    for line in str(value if value is not None else "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _lead_summary(type_: object, payload: dict) -> str:
    """The type-specific lead text for *type_*, or ``""`` when it has none."""
    if type_ == ev_types.AC_QA_VERDICT:
        verdict = _first_line(payload.get("verdict")).upper()
        head = f"AC {payload.get('ac_index', '?')} {verdict}".rstrip()
        evidence = _first_line(payload.get("evidence"))
        return f"{head}: {evidence}" if evidence else head
    if type_ == ev_types.AC_VERIFIED:
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            what, where, result = (_first_line(evidence.get(k)) for k in ("what", "where", "result"))
            return f"AC {payload.get('ac_index', '?')}: {what} @ {where} → {result}"
        return f"AC {payload.get('ac_index', '?')}: {_first_line(evidence)}"
    if type_ == ev_types.REVIEW_FEEDBACK_RECEIVED:
        author, path = (_first_line(payload.get(k)) for k in ("author", "path"))
        return f"{author} {path}:{payload.get('line', '')} {_first_line(payload.get('body'))}".strip()
    for key in _LEAD_KEYS.get(type_, ()):
        text = _first_line(payload.get(key))
        if text:
            return text
    return ""


def event_summary(ev: dict) -> str:
    """Plain-text one-line payload summary: lead fields for the types in ``_LEAD_KEYS``
    and the verdict / evidence / feedback types, else the first three ``k=v`` pairs."""
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    type_ = ev.get("type")
    if type_ == _IMPL_STEP:
        return f"{payload.get('kind', 'note')} {_first_line(payload.get('summary', ''))[:80]}"
    text = _lead_summary(type_, payload)
    if not text:
        text = _first_line(", ".join(f"{k}={v}" for k, v in list(payload.items())[:3]))
    return text if len(text) <= _SUMMARY_CAP else text[:_SUMMARY_CAP - 1] + "…"


def _complete_lines(data: bytes) -> tuple[list[dict], int]:
    """Parse the complete (``\\n``-terminated) lines of ``data``; return the
    events and the byte length consumed. Unparseable complete lines are skipped."""
    end = data.rfind(b"\n") + 1
    out: list[dict] = []
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = _json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
            out.append(obj)
    return out, end


class EventTail:
    """Incremental, read-only reader of every ``events/<KEY>.jsonl`` under a home.

    Per key it keeps ``(inode, offset, max_seq)``. ``poll()`` returns events not
    yet returned, each exactly once, surviving ``ops.compact`` (inode change /
    shrink -> re-read archive + new active log, filtered by ``max_seq``) and torn
    last lines (only ``\\n``-terminated lines are consumed)."""

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._state: dict[str, tuple[int | None, int, int]] = {}

    def _keys(self) -> list[str]:
        d = self._home / "events"
        if not d.is_dir():
            return []
        return sorted(f.name[:-len(".jsonl")] for f in d.iterdir()
                      if f.name.endswith(".jsonl") and not f.name.endswith(".archive.jsonl")
                      and not f.name.startswith("."))

    def backfill(self, limit: int = 200) -> list[dict]:
        """The last ``limit`` events board-wide by ``ts`` (oldest first). Seeds each
        key's ``max_seq`` and starts its tail at offset 0 so anything appended after
        this read is still picked up by the first ``poll()``."""
        found: list[dict] = []
        for key in self._keys():
            try:
                inode = store.events_path(self._home, key).stat().st_ino  # before the read
                evs = event_log.read(self._home, key)
            except (OSError, ValueError):
                continue
            self._state[key] = (inode, 0, max((e["seq"] for e in evs), default=0))
            found += [{**e, "key": key} for e in evs]
        found.sort(key=lambda e: (e.get("ts") or "", e["key"], e["seq"]))
        return found[-limit:]

    def poll(self) -> list[dict]:
        out: list[dict] = []
        live = set(self._keys())
        for gone in set(self._state) - live:
            del self._state[gone]
        for key in sorted(live):
            try:
                out += self._poll_key(key)
            except (OSError, ValueError):
                continue  # vanished mid-poll (archived) or invalid key; next tick decides
        out.sort(key=lambda e: (e.get("ts") or "", e["key"], e["seq"]))
        return out

    def _poll_key(self, key: str) -> list[dict]:
        inode, offset, max_seq = self._state.get(key, (None, 0, 0))
        path = store.events_path(self._home, key)
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            rotated = (inode is not None and st.st_ino != inode) or st.st_size < offset
            if rotated:
                data = fh.read()  # active first, archive second: a racing compaction only duplicates
                offset = 0
            else:
                fh.seek(offset)
                data = fh.read()
        evs, consumed = _complete_lines(data)
        if rotated:
            merged = {e["seq"]: e for e in store.read_jsonl(store.events_archive_path(self._home, key))
                      if isinstance(e.get("seq"), int)}
            merged.update((e["seq"], e) for e in evs)
            evs = [merged[s] for s in sorted(merged)]
        fresh = [{**e, "key": key} for e in evs if e["seq"] > max_seq]
        self._state[key] = (st.st_ino, offset + consumed,
                            max([max_seq] + [e["seq"] for e in fresh]))
        return fresh
