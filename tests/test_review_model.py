"""review.load: statuses, legacy timestamps, totality over Phase."""
from __future__ import annotations

import pytest

from maestro import event_log, review, store
from maestro import snapshot as snap_mod
from maestro.statemachine import Phase

SHA = "a" * 40
# 2026-01-01T00:00:00Z == 1767225600
PERMALINK = "https://x.slack.com/archives/C123ABC/p1767225600123456"


def seed(home, key, phase, *, round_source=None, round_=True, questions=None, brief=False,
         pending=False, reason="", external_id="acme/widgets#7", repo="widgets"):
    store.atomic_write(store.spec_path(home, key), f"# {key}\n")
    event_log.append(home, key, "TicketCreated", {
        "title": key, "kind": "review", "repo": repo, "external_id": external_id,
        "external_source": "github-pr"}, actor="d")
    if round_:
        p = {"head_sha": SHA, "url": "https://github.com/acme/widgets/pull/7"}
        if round_source:
            p["source"] = round_source
        event_log.append(home, key, "ReviewRoundStarted", p, actor="human")
    if brief:
        event_log.append(home, key, "ReviewBriefWritten", {
            "head_sha": SHA, "summary": "s", "verdict": "approve"}, actor="r")
    if pending:
        event_log.append(home, key, "ReviewPendingCreated",
                         {"review_id": 1, "pr_number": 7, "head_sha": SHA}, actor="r")
    for qid in questions or []:
        event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": "?"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": reason}, actor="r")
    snap_mod.rebuild(home, key)
    return review.load(home, key)


@pytest.mark.parametrize("phase,kw,status", [
    ("ready", {}, "queued"),
    ("verifying", {}, "testing"),
    ("qa", {}, "reviewing"),
    ("awaiting-human", {"questions": ["review-brief-k-abc"], "brief": True}, "brief-ready"),
    ("awaiting-ci", {"pending": True}, "pending-on-github"),
    ("done", {"reason": "review submitted"}, "closed"),
    ("degraded", {}, "failed"),
    ("ready", {"brief": True}, "stranded"),
])
def test_status(home, phase, kw, status):
    assert seed(home, "r-1", phase, **kw).status.value == status


def test_status_total_over_phase(home):
    for ph in Phase:
        s = snap_mod.Snapshot(key="k", phase=ph.value, kind="review")
        assert isinstance(review.status_of(s), review.ReviewStatus)


def test_blocked_unbound(home):
    r = seed(home, "r-2", "awaiting-human", questions=["review-unbound-r-2"], round_=False)
    assert r.status is review.ReviewStatus.BLOCKED and r.needs_you
    assert (r.slug, r.number) == ("acme/widgets", 7)


def test_requested_at_from_permalink(home):
    r = seed(home, "r-3", "ready", round_source=PERMALINK)
    assert r.requested_at.startswith("2026-01-01T00:00:00") and not r.requested_at_inferred


def test_requested_at_inferred(home):
    r = seed(home, "r-4", "ready", round_source="not a link")
    created = event_log.read(home, "r-4")[0]["ts"]
    assert r.requested_at == created and r.requested_at_inferred


def test_outcome(home):
    assert seed(home, "r-5", "done", reason="review submitted").outcome == "submitted"
    assert seed(home, "r-6", "done", reason="whatever").outcome == "closed"
