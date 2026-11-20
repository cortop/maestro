"""The one module that knows what a review is: model, status vocabulary and loaders."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .. import event_log, events as E, snapshot as snap_mod
from ..statemachine import Phase

_PERMALINK_RE = re.compile(r"/archives/[A-Za-z0-9]+/p(\d{16})(?!\d)")
_SLUG_RE = re.compile(r"^(?P<slug>[^#\s]+)#(?P<number>\d+)$")
_URL_RE = re.compile(r"github\.com/(?P<slug>[^/\s]+/[^/\s]+)/pull/(?P<number>\d+)")
SEVERITIES = ("blocking", "question", "nit")


class ReviewStatus(str, Enum):
    BLOCKED = "blocked"
    QUEUED = "queued"
    TESTING = "testing"
    REVIEWING = "reviewing"
    BRIEF_READY = "brief-ready"
    STRANDED = "stranded"
    PENDING_ON_GITHUB = "pending-on-github"
    CLOSED = "closed"
    FAILED = "failed"
    OTHER = "other"


_NEEDS_YOU_STATUSES = frozenset({ReviewStatus.BRIEF_READY, ReviewStatus.BLOCKED,
                                 ReviewStatus.STRANDED, ReviewStatus.FAILED})
_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})


@dataclass(frozen=True)
class Review:
    key: str
    slug: str | None
    number: int | None
    url: str | None
    repo: str | None
    author: str | None
    head_sha: str | None
    round: int
    status: ReviewStatus
    needs_you: bool
    findings: dict
    verdict: str | None
    pending: dict | None
    source: str | None
    requested_at: str | None
    requested_at_inferred: bool
    requested_by: str | None
    outcome: str | None
    requests: tuple = ()
    via: str | None = None
    where_label: str | None = None
    where_url: str | None = None
    last_request: dict | None = None
    request_count: int = 0
    title: str | None = None
    pr_state: str | None = None
    draft: bool | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    closed_at: str | None = None
    review_state: str | None = None
    previous_key: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["requests"] = list(d["requests"])
        d["status"] = self.status.value
        return d


def is_review(snap) -> bool:
    return getattr(snap, "kind", None) == "review"


def split(snaps) -> tuple[list, list]:
    """(tickets, reviews) of *snaps*, order preserved."""
    tickets: list = []
    reviews: list = []
    for s in snaps:
        (reviews if is_review(s) else tickets).append(s)
    return tickets, reviews


def _open_qids(snap) -> list[str]:
    return [q for q in (snap.open_questions or {}) if isinstance(q, str)]


def status_of(snap) -> ReviewStatus:
    """The review-terms status of *snap*; total over every `Phase`."""
    try:
        phase = Phase(snap.phase)
    except ValueError:
        return ReviewStatus.OTHER
    qids = _open_qids(snap)
    brief = snap.review_brief or {}
    brief_current = bool(brief) and brief.get("head_sha") == snap.review_head_sha
    if phase == Phase.AWAITING_HUMAN:
        if any(q.startswith(("review-unbound-", "review-fork-")) for q in qids):
            return ReviewStatus.BLOCKED
        if any(q.startswith("review-brief-") for q in qids):
            return ReviewStatus.BRIEF_READY
    elif phase in (Phase.TRIAGING, Phase.READY):
        if phase == Phase.READY and brief_current and not qids:
            return ReviewStatus.STRANDED
        if snap.review_round and not brief_current:
            return ReviewStatus.QUEUED
    elif phase == Phase.VERIFYING:
        return ReviewStatus.TESTING
    elif phase == Phase.QA:
        return ReviewStatus.REVIEWING
    elif phase == Phase.AWAITING_CI:
        if snap.review_pending:
            return ReviewStatus.PENDING_ON_GITHUB
    elif phase == Phase.DONE:
        return ReviewStatus.CLOSED
    elif phase == Phase.DEGRADED:
        return ReviewStatus.FAILED
    return ReviewStatus.OTHER


def _slug_number(snap) -> tuple[str | None, int | None]:
    for text, rx in ((snap.review_url, _URL_RE), (snap.external_id, _SLUG_RE)):
        m = rx.search(text) if isinstance(text, str) else None
        if m:
            return m.group("slug"), int(m.group("number"))
    for text, rx in ((snap.external_id, _SLUG_RE), (snap.review_url, _URL_RE)):
        m = rx.search(text) if isinstance(text, str) else None
        if m:
            return m.group("slug"), int(m.group("number"))
    return None, None


def _permalink_time(source) -> str | None:
    m = _PERMALINK_RE.search(source) if isinstance(source, str) else None
    if not m:
        return None
    digits = m.group(1)
    try:
        dt = datetime.fromtimestamp(int(digits[:10]), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.replace(microsecond=int(digits[10:])).isoformat()


def _who(requester) -> str | None:
    if not isinstance(requester, dict):
        return None
    return requester.get("name") or requester.get("id") or None


def _where_label(via, where) -> str | None:
    where = where if isinstance(where, dict) else {}
    if via == "slack":
        return f"Slack #{where['channel']}" if where.get("channel") else "Slack"
    return where.get("surface") or via


def _log_facts(home: Path, key: str) -> tuple[str | None, bool, str | None, list[dict], dict, dict]:
    """(requested_at, inferred, last PhaseChanged reason, requests, last PR observation,
    last `ReviewClosed` as {outcome, review_state, ts}) from one pass over the log."""
    created = None
    requests: list[dict] = []
    pr_obs: dict = {}
    closed: dict = {}
    first_round_seen = False
    requested = None
    last_reason = None
    try:
        evs = event_log.read(home, key)
    except Exception:
        evs = []
    for e in evs:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if t == E.TICKET_CREATED and created is None:
            created = e.get("ts")
        elif t == E.REVIEW_ROUND_STARTED and not first_round_seen:
            first_round_seen = True
            requested = _permalink_time(p.get("source"))
        elif t == E.PHASE_CHANGED:
            last_reason = p.get("reason")
        elif t == E.REVIEW_REQUESTED:
            requests.append(p)
        elif t == E.REVIEW_PR_OBSERVED:
            pr_obs = p
        elif t == E.REVIEW_CLOSED:
            closed = {"outcome": p.get("outcome"), "review_state": p.get("review_state"),
                      "ts": e.get("ts")}
    first_ts = requests[0].get("requested_ts") if requests else None
    if first_ts:
        return first_ts, False, last_reason, requests, pr_obs, closed
    if requested:
        return requested, False, last_reason, requests, pr_obs, closed
    return created, True, last_reason, requests, pr_obs, closed


def _outcome(reason) -> str:
    low = (reason or "").lower() if isinstance(reason, str) else ""
    for word, name in (("submitted", "submitted"), ("merged", "merged"),
                       ("marked reviewed", "marked reviewed"), ("closed", "closed")):
        if word in low:
            return name
    return "closed"


def _counts(snap) -> dict:
    counts = {s: 0 for s in SEVERITIES}
    for c in snap.review_comments or []:
        sev = c.get("severity") if isinstance(c, dict) else None
        if sev in counts:
            counts[sev] += 1
    return counts


def build(home: Path, snap) -> Review:
    status = status_of(snap)
    slug, number = _slug_number(snap)
    requested_at, inferred, reason, requests, pr, closed = _log_facts(home, snap.key)
    try:
        in_needs_phase = Phase(snap.phase) in _NEEDS_YOU_PHASES
    except ValueError:
        in_needs_phase = False
    needs = status in _NEEDS_YOU_STATUSES or (status == ReviewStatus.OTHER and in_needs_phase)
    brief = snap.review_brief or {}
    first = requests[0] if requests else {}
    where = first.get("where") if isinstance(first.get("where"), dict) else {}
    return Review(
        key=snap.key, slug=slug, number=number,
        url=snap.review_url or (f"https://github.com/{slug}/pull/{number}" if slug else None),
        repo=getattr(snap, "repo", None), author=snap.review_author,
        head_sha=snap.review_head_sha, round=snap.review_round, status=status,
        needs_you=needs, findings=_counts(snap), verdict=brief.get("verdict"),
        pending=snap.review_pending, source=snap.review_source,
        requested_at=requested_at, requested_at_inferred=inferred,
        requested_by=_who(first.get("requester")),
        outcome=((closed.get("outcome") or _outcome(reason))
                 if status == ReviewStatus.CLOSED else None),
        requests=tuple(requests), via=first.get("via"),
        where_label=_where_label(first.get("via"), where) if first else None,
        where_url=where.get("permalink"),
        last_request=requests[-1] if requests else None, request_count=len(requests),
        title=pr.get("title"), pr_state=pr.get("state"), draft=pr.get("draft"),
        additions=pr.get("additions"), deletions=pr.get("deletions"),
        changed_files=pr.get("changed_files"),
        closed_at=closed.get("ts") if status == ReviewStatus.CLOSED else None,
        review_state=closed.get("review_state"), previous_key=first.get("previous_key"))


def load(home: Path, key: str) -> Review:
    return build(home, snap_mod.load(home, key))


def list_reviews(home: Path, include_closed: bool = False) -> list[Review]:
    from ..dispatcher import list_keys  # lazy: dispatcher imports much of the package
    out = []
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        if not is_review(snap):
            continue
        r = build(home, snap)
        if include_closed or r.status != ReviewStatus.CLOSED:
            out.append(r)
    return out
