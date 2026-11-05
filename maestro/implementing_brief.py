"""Classifies the current implementing round and assembles the brief a reconciler loads for it."""
from __future__ import annotations

from pathlib import Path

from . import event_log
from . import events as E
from . import outcomes

ROUNDS = ("fresh", "qa-fix", "ci-fix", "review-feedback", "conflict", "base-drift", "split-decision")
# `sync` is the shared base-sync steps; `--round sync` prints them alone.
OVERRIDES = ROUNDS + ("sync",)
_ROUNDS_DIR = Path(__file__).resolve().parent / "_implementing_rounds"
_FILES = {"conflict": ("sync", "conflict"), "base-drift": ("sync", "base-drift")}


def current_round(home: Path, key: str) -> tuple[str, str]:
    """(round, reason) for the most recent transition into implementing."""
    prev = None
    found: tuple[str, str] = ("unknown", "")
    for ev in event_log.read(home, key):
        if ev["type"] != E.PHASE_CHANGED:
            continue
        new = ev.get("payload", {}).get("phase")
        if new == "implementing":
            reason = ev["payload"].get("reason") or ""
            found = (outcomes.implementing_round(reason, prev), reason)
        prev = new
    return found


def instructions(round_name: str) -> str:
    """The instruction text for *round_name*; "unknown" (and `sync`) get every round's."""
    if round_name == "unknown":
        names = ["sync", *ROUNDS]
    else:
        names = list(_FILES.get(round_name, (round_name,)))
    return "\n\n".join((_ROUNDS_DIR / f"{n}.md").read_text(encoding="utf-8").strip() for n in names)
