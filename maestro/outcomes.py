"""Read-only fold of ticket event logs into per-repo/per-month outcome figures (`maestro scorecard tickets`).

Every figure carries its n, its excluded rows and a Wilson interval. The module
never appends an event, writes a snapshot or touches a derived file.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import event_log
from . import events as E
from . import repos as repos_mod
from .config import Config
from .dispatcher import list_keys

Z95 = 1.959963984540054
CYCLE_FAST_HOURS = 24.0
DEFAULT_REPO = "default"
ROUND_CAUSES = ("review", "qa", "ci", "tests", "conflict", "other")
_REVIEW_REASON_RE = re.compile(r"^(?:PR #\d+: )?(?:changes requested|review comment|approved with comments):")


def wilson(k: int, n: int, z: float = Z95) -> list[float] | None:
    """Wilson score interval for *k* successes in *n* trials; None when n is 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _figure(k: int, n: int, excluded: int, **extra) -> dict:
    return {"k": k, "n": n, "excluded": excluded,
            "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n), **extra}


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def _round_cause(reason: str, prev_phase: str | None) -> str:
    if _REVIEW_REASON_RE.match(reason):
        return "review"
    if prev_phase == "qa":
        return "qa"
    if re.match(r"^(?:PR #\d+: )?CI failing", reason):
        return "ci"
    if reason.startswith(("tests failed", "annotated AC check")):
        return "tests"
    if "conflict" in reason.lower() or "advanced" in reason:
        return "conflict"
    return "other"


def is_bot(cfg: Config | None, author: str | None) -> bool | None:
    """True/False for a classifiable review author; None when there is no author."""
    if not author:
        return None
    if author.endswith("[bot]"):
        return True
    return bool(cfg is not None and author in cfg.review_noise_authors)


def fold_ticket(cfg: Config | None, home: Path, key: str, cutoff: datetime | None) -> dict | None:
    """One ticket's outcome facts as of *cutoff*; None when it has no TicketCreated by then."""
    events = event_log.read(home, key)
    if cutoff is not None:
        events = [e for e in events if _parse_ts(e["ts"]) <= cutoff]
    created = next((e for e in events if e["type"] == E.TICKET_CREATED), None)
    if created is None:
        return None
    t0 = _parse_ts(created["ts"])
    repo = repos_mod.bound_repo_name(home, key) or created.get("payload", {}).get("repo") or DEFAULT_REPO
    phase = None
    merged_at = None
    first_verdict: dict[str, str] = {}
    rounds: list[dict] = []
    seen_ticks: set[tuple[str, str]] = set()
    reviews: list[bool | None] = []
    for ev in events:
        typ, pl = ev["type"], ev.get("payload", {})
        if typ == E.PHASE_CHANGED:
            new = pl.get("phase")
            if rounds and rounds[-1]["next"] is None:
                rounds[-1]["next"] = new
            if new == "implementing":
                cause = _round_cause(pl.get("reason") or "", phase)
                tick = (cause, ev["ts"])
                if cause == "review" and tick in seen_ticks:
                    pass  # same-tick duplicate review route: counted once
                elif cause == "review" or phase != "implementing":
                    seen_ticks.add(tick)
                    rounds.append({"cause": cause, "next": None})
            phase = new
        elif typ == E.PR_UPDATED and pl.get("merged") and "stack_index" not in pl:
            merged_at = merged_at or _parse_ts(ev["ts"])
        elif typ == E.AC_QA_VERDICT and pl.get("axis", "spec") == "spec":
            first_verdict.setdefault(pl.get("ac_hash", ""), pl.get("verdict", ""))
        elif typ == E.REVIEW_FEEDBACK_RECEIVED:
            reviews.append(is_bot(cfg, pl.get("author")))
    if merged_at is not None:
        terminal = "merged"
    elif phase == "done":
        terminal = "done-unmerged"
    elif phase == "degraded":
        terminal = "degraded"
    else:
        terminal = None
    return {"key": key, "repo": repo, "month": t0.strftime("%Y-%m"), "terminal": terminal,
            "first_pass_qa": (all(v == "pass" for v in first_verdict.values())
                              if first_verdict else None),
            "rounds": rounds, "reviews": reviews,
            "cycle_hours": ((merged_at - t0).total_seconds() / 3600 if merged_at else None)}


def _group_metrics(tickets: list[dict]) -> dict:
    n_all = len(tickets)
    done = [t for t in tickets if t["terminal"] is not None]
    merged = [t for t in tickets if t["terminal"] == "merged"]
    qa = [t for t in tickets if t["first_pass_qa"] is not None]
    by_cause = {}
    for cause in ROUND_CAUSES:
        k = sum(1 for t in tickets if any(r["cause"] == cause for r in t["rounds"]))
        by_cause[cause] = _figure(k, n_all, 0, rounds=sum(
            1 for t in tickets for r in t["rounds"] if r["cause"] == cause))
    unqad = sum(1 for t in merged
                if any(r["next"] != "qa" for r in t["rounds"]))
    unqad_rounds = sum(1 for t in merged for r in t["rounds"]
                       if r["next"] != "qa")
    cycles = sorted(t["cycle_hours"] for t in merged)
    fast = sum(1 for c in cycles if c <= CYCLE_FAST_HOURS)
    revs = [b for t in tickets for b in t["reviews"]]
    classified = [b for b in revs if b is not None]
    return {
        "tickets": n_all,
        "terminal_merged": _figure(len(merged), len(done), n_all - len(done)),
        "first_pass_qa": _figure(sum(1 for t in qa if t["first_pass_qa"]), len(qa), n_all - len(qa)),
        "rounds_by_cause": by_cause,
        "unqad_rounds_before_merge": _figure(unqad, len(merged), n_all - len(merged),
                                             rounds=unqad_rounds),
        "cycle_time": _figure(fast, len(merged), n_all - len(merged),
                              within_hours=CYCLE_FAST_HOURS,
                              median_hours=round(statistics.median(cycles), 2) if cycles else None),
        "reviews_bot": _figure(sum(1 for b in classified if b), len(classified),
                               len(revs) - len(classified)),
    }


def scorecard(cfg: Config, *, cutoff: datetime | None = None) -> dict:
    """Per-repo, per-month outcome figures over every ticket log. Read-only."""
    tickets = [t for k in list_keys(cfg.home)
               if (t := fold_ticket(cfg, cfg.home, k, cutoff)) is not None]
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in tickets:
        groups.setdefault((t["repo"], t["month"]), []).append(t)
    return {"cutoff": cutoff.isoformat() if cutoff else None, "tickets": len(tickets),
            "groups": [{"repo": repo, "month": month, **_group_metrics(ts)}
                       for (repo, month), ts in sorted(groups.items())]}
