"""T-125 (a): a pure stdlib fold of the answer -> route decision stream.

The board cannot yet evaluate any automatic routing -- nothing joins a human's
answer to what the reconciler then did with it. For each ``QuestionAnswered``,
this finds the next non-dispatcher ``PhaseChanged`` and classifies its reason
into a coarse label, producing the labeled answer->route stream
``derived/labels/answers.jsonl`` -- what finally makes T-122's shadow
``would_route_answer``/``answer_routed`` ledger outcomes auditable from events
instead of "by eye". Read-only throughout: ``regenerate`` overwrites the jsonl
(a disposable projection, same posture as ``derived/context/*.md``) but never
appends an event.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import event_log, store
from . import events as E
from .dispatcher import dispatch_ledger_path, list_keys

_QID_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")

# T-122's shadow/live ledger outcomes this fold joins against, keyed by
# (key, qid) -- see `_ledger_routes` below. T-122 is independent and
# currently unmerged; this module reads whatever shape it eventually writes
# and degrades to `agreement() -> None` (nothing to report) until it exists.
_LEDGER_OUTCOMES = ("would_route_answer", "answer_routed")


def _qid_class(qid: str) -> str:
    return "content_hash" if _QID_CONTENT_HASH_RE.fullmatch(qid or "") else "named"


def _classify_reason(reason: str) -> str:
    """The spec's exact prefix/substring rules, in priority order. Anything
    that matches none of them is "unclassified" rather than silently folded
    into "other" -- the spec reserves "other" for exactly the two named
    phrases ("needs more research" / "retry conflict resolution"), not as a
    catch-all."""
    if reason.startswith("approved:"):
        return "approve"
    if reason.startswith("rejected:"):
        return "reject"
    if "modified scope" in reason:
        return "modify"
    if "needs more research" in reason or "retry conflict resolution" in reason:
        return "other"
    return "unclassified"


def fold_answers(key: str, events: list[dict]) -> list[dict]:
    """One labeled row per ``QuestionAnswered`` in *events* -- ``{key, qid,
    qid_class, answer, label, phase_before, phase_after, ts}`` -- each matched
    to the next ``PhaseChanged`` whose ``actor`` is not ``"dispatcher"``: the
    routing decision the answer actually produced (an automatic dispatcher
    reroute landing in the same window, e.g. a review-feedback bounce, is not
    that decision and is skipped over, left for a LATER answer/PhaseChanged
    pair to match against). A trailing answer with no matching PhaseChanged
    yet (still `awaiting-human` at fold time) is omitted -- there is nothing
    to label yet; the fold is re-run any time via `regenerate`, so it appears
    the moment its route lands.
    """
    asked: dict[str, str] = {}
    pending: list[dict] = []
    current_phase: str | None = None
    rows: list[dict] = []
    for ev in events:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == E.QUESTION_ASKED:
            asked[p.get("qid", "")] = p.get("text", "")
        elif t == E.QUESTION_ANSWERED:
            qid = p.get("qid", "")
            pending.append({
                "key": key,
                "qid": qid,
                "qid_class": _qid_class(qid),
                "answer": p.get("answer", ""),
                "label": None,
                "phase_before": current_phase,
                "phase_after": None,
                "ts": ev.get("ts", ""),
            })
        elif t == E.PHASE_CHANGED:
            new_phase = p.get("phase", "")
            if ev.get("actor") != "dispatcher" and pending:
                row = pending.pop(0)
                row["phase_after"] = new_phase
                row["label"] = _classify_reason(p.get("reason", ""))
                rows.append(row)
            current_phase = new_phase
    return rows


def label_answers(home: Path) -> list[dict]:
    """Every ticket's answer rows, folded fresh and sorted chronologically."""
    rows: list[dict] = []
    for key in list_keys(home):
        rows.extend(fold_answers(key, event_log.read(home, key)))
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def answers_path(home: Path) -> Path:
    return home / "derived" / "labels" / "answers.jsonl"


def regenerate(home: Path) -> list[dict]:
    """Fold every ticket's log into labeled answer rows and atomically persist
    them to ``derived/labels/answers.jsonl``. Read-only otherwise -- never
    appends an event, never mutates a snapshot."""
    rows = label_answers(home)
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    store.atomic_write(answers_path(home), text)
    return rows


def label_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    return counts


def _ledger_routes(home: Path) -> dict[tuple[str, str], str]:
    """``(key, qid) -> route`` for the most recently recorded
    ``would_route_answer``/``answer_routed`` decision in the dispatch ledger
    (``derived/dispatch.jsonl``). The ledger's existing ``decisions`` dict is
    keyed by ticket key only (one entry per key per sweep), so the qid has to
    travel inside the decision payload itself to join back to one specific
    answer -- this reads ``decision["qid"]``/``decision["route"]`` off any
    decision whose ``outcome`` is one of ``_LEDGER_OUTCOMES``. Later ledger
    records overwrite earlier ones for the same ``(key, qid)``, so a
    re-shadowed sweep's freshest guess wins.
    """
    routes: dict[tuple[str, str], str] = {}
    for record in store.read_jsonl(dispatch_ledger_path(home)):
        for key, decision in (record.get("decisions") or {}).items():
            if not isinstance(decision, dict) or decision.get("outcome") not in _LEDGER_OUTCOMES:
                continue
            qid = decision.get("qid")
            route = decision.get("route")
            if qid and route:
                routes[(key, qid)] = route
    return routes


def agreement(rows: list[dict], home: Path) -> dict | None:
    """Agreement between each row's own ``label`` (the ACTUAL route a human's
    answer produced) and the ledger's ``would_route_answer``/
    ``answer_routed`` guess for that same ``(key, qid)``, in *rows* order
    (chronological, since ``label_answers`` sorts by ts). Returns ``None``
    when the ledger holds no such outcome at all for any row -- the "when
    present" gate ``cmd_scorecard`` applies before printing this section.
    """
    routes = _ledger_routes(home)
    if not routes:
        return None
    matched = [routes[(r["key"], r["qid"])] == r["label"]
               for r in rows if (r["key"], r["qid"]) in routes]
    if not matched:
        return None
    streak = 0
    for agree_here in reversed(matched):
        if not agree_here:
            break
        streak += 1
    return {
        "matched": len(matched),
        "agreements": sum(matched),
        "agreement_rate": sum(matched) / len(matched),
        "consecutive_streak": streak,
    }
