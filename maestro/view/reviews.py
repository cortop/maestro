"""Reviews view-model -- rows, groups, counts and detail of inbound PR reviews."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import event_log, events as E, projection, review, snapshot as snap_mod
from ..review import model as review_model
from ..config import Config
from . import board as board_view
from . import fleet as fleet_view

HISTORY_LIMIT = 20
# A review ticket never builds: its rail is the inbound-review pipeline, in its own words.
RAIL = (("ready", "queued"), ("verifying", "tests"), ("qa", "checking"),
        ("awaiting-human", "brief ready"), ("done", "submitted"))
GROUPS = ("needs_you", "in_progress", "on_github", "history")


@dataclass(frozen=True)
class ReviewRow:
    key: str
    title: str
    repo: str | None
    number: int | None
    url: str | None
    author: str | None
    requested_by: str | None
    requested_at: str | None
    requested_at_inferred: bool
    via: str | None
    where_label: str | None
    where_url: str | None
    request_count: int
    status: str
    status_sentence: str
    stale: bool
    round: int
    head_sha: str | None
    size: dict | None
    findings: dict
    verdict: str | None
    needs_you: bool
    waiting_s: float | None
    running: bool
    outcome: str | None = None
    closed_at: str | None = None


@dataclass
class ReviewsView:
    needs_you: list[ReviewRow] = field(default_factory=list)
    in_progress: list[ReviewRow] = field(default_factory=list)
    on_github: list[ReviewRow] = field(default_factory=list)
    history: list[ReviewRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    slack: dict = field(default_factory=dict)


@dataclass
class ReviewDetail:
    row: ReviewRow
    brief: dict | None
    findings: list[dict]
    requests: list[dict]
    timeline: list[dict]


def status_sentence(snap) -> str:
    """One line on who acts on a review and on what, from its snapshot alone."""
    status = review.status_of(snap)
    if status == review.ReviewStatus.CLOSED:
        return "Review finished."
    if status == review.ReviewStatus.FAILED or snap.phase == "degraded":
        return "Parked by the dispatcher" + (f": {snap.last_error}" if snap.last_error else ".")
    if status == review.ReviewStatus.BLOCKED:
        return "Blocked: waiting on you to answer."
    if status == review.ReviewStatus.PENDING_ON_GITHUB:
        return "The pending review is waiting on GitHub: submit it there."
    if status == review.ReviewStatus.STRANDED:
        return "The brief is ready: the dispatcher is waking the review to ask you."
    if snap.phase == "awaiting-human":
        if snap.review_brief:
            return "Brief ready: edit the comments, then waiting on your submit."
        return "Waiting on you to answer."
    if status == review.ReviewStatus.QUEUED:
        return "Queued: an agent will check the pull request."
    if snap.phase == "verifying":
        return "The dispatcher is running the PR's tests, then an agent checks it."
    return "An agent is checking the pull request."


def _group(snap) -> str:
    status = review.status_of(snap)
    if status == review.ReviewStatus.CLOSED:
        return "history"
    if review.needs_you_of(snap, status):
        return "needs_you"
    if status == review.ReviewStatus.PENDING_ON_GITHUB:
        return "on_github"
    return "in_progress"


def _epoch(ts) -> float | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _review_snaps(home: Path) -> list:
    return [s for s in projection.load_snapshots(home) if review.is_review(s)]


def _closed_ts(snap) -> float:
    return _epoch(snap.updated_ts) or 0.0


def _phase_times(events: list[dict]) -> tuple[str | None, str | None]:
    """(ts of the last phase change, ts of the last change to done)."""
    last = closed = None
    for e in events:
        if e.get("type") == E.PHASE_CHANGED:
            last = e.get("ts")
            if (e.get("payload") or {}).get("phase") == "done":
                closed = e.get("ts")
    return last, closed


def _row(home: Path, snap, now: float, running: frozenset[str]) -> ReviewRow:
    r = review_model.build(home, snap)
    try:
        events = event_log.read(home, snap.key)
    except Exception:
        events = []
    last, closed = _phase_times(events)
    status = r.status
    closed_row = status == review.ReviewStatus.CLOSED
    slug = r.slug or r.repo
    title = r.title or (f"{r.slug}#{r.number}" if r.slug and r.number else snap.title or snap.key)
    brief = snap.review_brief or {}
    size = None
    if r.additions is not None or r.deletions is not None or r.changed_files is not None:
        size = {"additions": r.additions, "deletions": r.deletions,
                "changed_files": r.changed_files}
    start = _epoch(last)
    return ReviewRow(
        key=r.key, title=title, repo=slug, number=r.number, url=r.url, author=r.author,
        requested_by=r.requested_by, requested_at=r.requested_at,
        requested_at_inferred=r.requested_at_inferred, via=r.via,
        where_label=r.where_label, where_url=r.where_url, request_count=r.request_count,
        status=status.value, status_sentence=status_sentence(snap),
        stale=bool(brief) and brief.get("head_sha") != snap.review_head_sha,
        round=r.round, head_sha=r.head_sha, size=size, findings=r.findings,
        verdict=r.verdict, needs_you=r.needs_you,
        waiting_s=None if closed_row or start is None else max(0.0, now - start),
        running=snap.key in running, outcome=r.outcome,
        closed_at=(closed or snap.updated_ts) if closed_row else None)


def _history_snaps(snaps: list, now: float, days: int) -> list:
    cutoff = now - timedelta(days=days).total_seconds()
    closed = [s for s in snaps if _group(s) == "history" and _closed_ts(s) >= cutoff]
    closed.sort(key=_closed_ts, reverse=True)
    return closed[:HISTORY_LIMIT]


def counts(home: Path, now: float | None = None, history_days: int = 14) -> dict[str, int]:
    """Group sizes from snapshots alone (no event log is read)."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    snaps = _review_snaps(home)
    out = {g: 0 for g in GROUPS}
    for s in snaps:
        g = _group(s)
        if g != "history":
            out[g] += 1
    out["history"] = len(_history_snaps(snaps, now, history_days))
    return out


def build(home: Path, cfg: Config, now: float, history_days: int = 14) -> ReviewsView:
    """The four groups; history rows are chosen and cut from snapshots before any log is read."""
    snaps = _review_snaps(home)
    running = board_view.running_keys(home, cfg)
    by_group: dict[str, list] = {g: [] for g in GROUPS}
    for s in snaps:
        g = _group(s)
        if g != "history":
            by_group[g].append(s)
    by_group["history"] = _history_snaps(snaps, now, history_days)
    view = ReviewsView(slack=fleet_view.slack_panel(cfg, now))
    for g in GROUPS:
        rows = [_row(home, s, now, running) for s in by_group[g]]
        if g == "needs_you":
            rows.sort(key=lambda r: -(r.waiting_s or 0.0))
        elif g != "history":
            rows.sort(key=lambda r: r.requested_at or "")
        setattr(view, g, rows)
        view.counts[g] = len(rows)
    return view


_TIMELINE = (
    (E.REVIEW_REQUESTED, "asked"), (E.TICKET_CREATED, "filed"),
    (E.REVIEW_ROUND_STARTED, "round started"), (E.TEST_RUN_CAPTURED, "tests done"),
    (E.REVIEW_BRIEF_WRITTEN, "brief ready"), (E.REVIEW_PENDING_CREATED, "pending created"))


def _timeline(events: list[dict]) -> list[dict]:
    labels = dict(_TIMELINE)
    out = []
    for e in events:
        t = e.get("type")
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if t in labels:
            label = labels[t]
        elif t == E.PHASE_CHANGED and payload.get("phase") == "done":
            label = "closed"
        else:
            continue
        out.append({"label": label, "ts": e.get("ts"), "seq": e.get("seq"), "type": t})
    return out


def detail(home: Path, cfg: Config, key: str, now: float) -> ReviewDetail | None:
    """One row plus brief, findings, requests and an event-timestamped timeline;
    ``None`` when *key* is not a review."""
    snap = snap_mod.load(home, key)
    if not review.is_review(snap):
        return None
    row = _row(home, snap, now, board_view.running_keys(home, cfg))
    try:
        events = event_log.read(home, key)
    except Exception:
        events = []
    return ReviewDetail(
        row=row, brief=snap.review_brief, findings=list(snap.review_comments or []),
        requests=[dict(p) for p in (review.load(home, key).requests)],
        timeline=_timeline(events))
