"""Plain-words label for why a ticket's session is running (round and trigger)."""
from __future__ import annotations

from pathlib import Path

from .. import config as config_mod, event_log, implementing_brief, outcomes, snapshot as snap_mod, store
from .. import events as E

_PHASE_LABELS = {
    "triaging": "Triaging",
    "researching": "Researching",
    "ready": "Setting up",
    "awaiting-human": "Applying your answer",
}


def _feedback_window_start(events: list[dict]) -> int:
    """Seq of the phase change before the latest one into implementing: review comments
    arrive while the ticket waits in review, so the window opens there."""
    changes = [ev["seq"] for ev in events if ev["type"] == E.PHASE_CHANGED]
    return changes[-2] if len(changes) > 1 else 0


def _implementing_label(home: Path, key: str, events: list[dict]) -> str:
    name, _reason = implementing_brief.current_round(home, key)
    if name == "review-feedback":
        try:
            cfg = config_mod.load(str(home))
        except store.MaestroError:
            cfg = None
        start = _feedback_window_start(events)
        n = sum(1 for ev in events if ev["type"] == E.REVIEW_FEEDBACK_RECEIVED and ev["seq"] > start
                and not outcomes.is_bot(cfg, ev.get("payload", {}).get("author")))
        return f"Addressing review feedback · {n} comment{'' if n == 1 else 's'}"
    if name == "qa-fix":
        fails = [ev for ev in events if ev["type"] == E.AC_QA_VERDICT
                 and ev.get("payload", {}).get("verdict") == "fail"]
        return f"Fixing QA failure · AC {fails[-1]['payload'].get('ac_index')}" if fails else "Fixing QA failure"
    if name == "ci-fix":
        cis = [ev for ev in events if ev["type"] == E.CI_OBSERVED]
        failing = (cis[-1].get("payload", {}).get("failing_checks") or []) if cis else []
        return f"Fixing CI · {failing[0]}" if failing else "Fixing CI"
    return {
        "conflict": "Resolving merge conflict",
        "base-drift": "Syncing with base",
        "split-decision": "Splitting the PR",
        "fresh": "Implementing the ticket",
    }.get(name, "Implementing (your message)")


def round_label(home: Path, key: str) -> str:
    """One line naming the round the ticket's session works on and its trigger; empty for a
    phase no agent session handles."""
    snap = snap_mod.load(home, key)
    events = event_log.read(home, key)
    if events and events[-1]["type"] == E.POST_QA_SKILL_SPAWNED:
        return "Polishing the PR"
    if snap.phase == "implementing":
        return _implementing_label(home, key, events)
    if snap.phase == "qa":
        spec = store.spec_path(home, key)
        n = len(snap_mod.parse_acs(spec.read_text(encoding="utf-8"))) if spec.exists() else 0
        return f"QA · checking {n} AC{'' if n == 1 else 's'}"
    return _PHASE_LABELS.get(snap.phase, "")
