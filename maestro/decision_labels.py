"""T-125 (a): a pure stdlib fold of the answer -> route decision stream.

The board cannot yet evaluate any automatic routing -- nothing joins a human's
answer to what the reconciler then did with it. For each ``QuestionAnswered``,
this finds the round's actual route -- the next ``PhaseChanged`` whose actor
isn't ``"dispatcher"``, or one that is but whose reason marks a T-122
``answer_fast_path`` route -- and classifies its reason into a coarse label,
producing the labeled answer->route stream ``derived/labels/answers.jsonl``
-- what finally makes T-122's shadow ``would_route_answer``/``answer_routed``
decisions (``events.FAST_PATH_DECIDED``) auditable from events instead of "by
eye". Read-only throughout: ``regenerate`` overwrites the jsonl
(a disposable projection, same posture as ``derived/context/*.md``) but never
appends an event.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import event_log, store
from . import events as E
from .dispatcher import list_keys

_QID_CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


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


def _is_route(payload: dict, actor: str | None) -> bool:
    """A ``PhaseChanged`` counts as the round's route if a human (or a
    reconciler acting on their behalf) drove it, OR -- T-122's
    `answer_fast_path = "on"` -- the dispatcher drove it itself but the
    reason marks an automatic fast-path approval (the same
    ``"approved: "``-prefix predicate `projection._recent_fast_path_routes`
    already uses). Any OTHER dispatcher-actor reroute (e.g. a review-feedback
    bounce landing in the same window) is not that decision -- skipped over,
    left for a later answer/PhaseChanged pair to match against."""
    if actor != "dispatcher":
        return True
    return payload.get("reason", "").startswith("approved: ")


def fold_answers(key: str, events: list[dict]) -> list[dict]:
    """One labeled row per ``QuestionAnswered`` in *events* -- ``{key, qid,
    qid_class, answer, label, phase_before, phase_after, ts}`` -- each matched
    to the round's route (see `_is_route`). A round can hold several
    ``QuestionAnswered`` events (one untargeted human ``ans`` folds to one per
    then-open qid) -- ALL of them are drained and labeled from that SAME
    route, not just the first, since they're all answers to the one round the
    route decided. A trailing answer with no matching route yet (still
    `awaiting-human` at fold time) is omitted -- there is nothing to label
    yet; the fold is re-run any time via `regenerate`, so it appears the
    moment its route lands.
    """
    pending: list[dict] = []
    current_phase: str | None = None
    rows: list[dict] = []
    for ev in events:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == E.QUESTION_ANSWERED:
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
            if pending and _is_route(p, ev.get("actor")):
                label = _classify_reason(p.get("reason", ""))
                for row in pending:
                    row["phase_after"] = new_phase
                    row["label"] = label
                    rows.append(row)
                pending = []
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


def _fast_path_decisions(home: Path) -> dict[tuple[str, str], str]:
    """``(key, qid) -> route`` for the most recently recorded T-122
    `events.FAST_PATH_DECIDED` decision, read directly from each ticket's own
    event log -- durable and uncapped, unlike `derived/dispatch.jsonl`'s
    500-line-per-sweep ledger (which this replaces as the join source: that
    ledger is trimmed/rebuilt continuously and was never meant as a durable
    store). Later events (higher seq) overwrite earlier ones for the same
    ``(key, qid)``, so a re-shadowed round's freshest guess wins.
    """
    routes: dict[tuple[str, str], str] = {}
    for key in list_keys(home):
        for ev in event_log.read(home, key):
            if ev.get("type") != E.FAST_PATH_DECIDED:
                continue
            p = ev.get("payload") or {}
            route = p.get("route")
            if not route:
                continue
            for qid in p.get("qid") or []:
                routes[(key, qid)] = route
    return routes


def agreement(rows: list[dict], home: Path) -> dict | None:
    """Agreement between each row's own ``label`` (the ACTUAL route a human's
    answer produced) and the durably recorded T-122 ``would_route_answer``/
    ``answer_routed`` guess for that same ``(key, qid)``, in *rows* order
    (chronological, since ``label_answers`` sorts by ts). Returns ``None``
    when no such decision is on record at all for any row -- the "when
    present" gate ``cmd_scorecard`` applies before printing this section.
    """
    routes = _fast_path_decisions(home)
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
