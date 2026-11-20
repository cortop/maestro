"""Web /reviews/<key> page: heading and panels, the one findings host, head fragment, actions, raw view, links."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import event_log  # noqa: E402
from maestro import snapshot as snap_mod  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402
from test_view_reviews import seed  # noqa: E402


def _client(home, writes=True):
    app = create_app(home, nudge_on_action=False, **({} if writes else {"read_only": True}))
    c = TestClient(app)
    if writes:
        c.headers[TOKEN_HEADER] = app.state.guard.token
    return c


@pytest.fixture
def web(home):
    seed(home, "r-need", "awaiting-human", questions=["review-brief-r-need-x"], brief=True, number=1)
    with _client(home) as c:
        yield c, home


def test_page_renders_heading_and_panels(web):
    c, _ = web
    r = c.get("/reviews/r-need")
    assert r.status_code == 200
    html = r.text
    assert '<h1 class="ticket-title">Fix parser</h1>' in html
    for want in ("Request", "Pull request", "Timeline", "Ada", "Slack #reviews", 'href="https://x/p1"',
                 "+10 −2", "3 files", "round 1", "Brief ready", 'class="current"'):
        assert want in html, want
    assert 'aria-current="page">Reviews' in html
    assert c.get("/reviews/nope").status_code == 404
    seed_non_review = c.get("/reviews/rows")  # the list fragment is not shadowed by the key route
    assert seed_non_review.status_code == 200


def test_non_review_ticket_is_404(home):
    from conftest import seed_ticket
    seed_ticket(home, "T-1", "plain")
    with _client(home) as c:
        assert c.get("/reviews/T-1").status_code == 404


def test_one_findings_host_without_sse(web):
    c, _ = web
    html = c.get("/reviews/r-need").text
    assert html.count('id="panel-review"') == 1
    tag = re.search(r'<div id="panel-review"[^>]*>', html).group(0)
    assert 'hx-get="/t/r-need/review"' in tag and 'hx-trigger="load"' in tag and "sse:" not in tag


def test_head_follows_status_after_pending_review(web):
    c, home = web
    assert "Brief ready" in c.get("/reviews/r-need/head").text or "brief ready" in c.get("/reviews/r-need/head").text
    event_log.append(home, "r-need", "ReviewPendingCreated",
                     {"review_id": 1, "pr_number": 1, "head_sha": "a" * 40}, actor="r")
    event_log.append(home, "r-need", "PhaseChanged", {"phase": "awaiting-ci", "reason": "pending"}, actor="r")
    snap_mod.rebuild(home, "r-need")
    assert "waiting on GitHub" in c.get("/reviews/r-need/head").text


def test_second_request_is_asked_again(web):
    c, home = web
    event_log.append(home, "r-need", "ReviewRequested", {
        "via": "slack", "requested_ts": "2026-05-31T10:09:00+00:00", "requester": {"name": "Sam"},
        "where": {"channel": "reviews", "permalink": "https://x/p2"}, "pr": {}}, actor="d")
    snap_mod.rebuild(home, "r-need")
    html = c.get("/reviews/r-need").text
    assert "Asked again" in html and "Sam" in html and 'href="https://x/p2"' in html


def test_rereview_and_retry_gating(home):
    seed(home, "r-stale", "awaiting-human", questions=["review-brief-r-stale-x"], brief=True, number=2)
    event_log.append(home, "r-stale", "ReviewPrObserved", {"title": "t", "head_sha": "b" * 40, "state": "open"}, actor="d")
    snap_mod.rebuild(home, "r-stale")
    seed(home, "r-block", "awaiting-human", questions=["review-unbound-r-block-x"], number=3)
    seed(home, "r-fresh", "awaiting-human", questions=["review-brief-r-fresh-x"], brief=True, number=4)
    with _client(home) as c:
        assert "/reviews/r-stale/rereview" in c.get("/reviews/r-stale/head").text
        assert "/reviews/r-stale/retry" not in c.get("/reviews/r-stale/head").text
        assert "/reviews/r-block/retry" in c.get("/reviews/r-block/head").text
        assert "/rereview" not in c.get("/reviews/r-block/head").text
        fresh = c.get("/reviews/r-fresh/head").text
        assert "/rereview" not in fresh and "/retry" not in fresh and "Mark reviewed" in fresh
    with _client(home, writes=False) as c:
        for k in ("r-stale", "r-block"):
            h = c.get(f"/reviews/{k}/head").text
            assert "Re-review" not in h and "Retry" not in h and "Mark reviewed" not in h


def test_mark_reviewed_from_the_page(web):
    c, home = web
    r = c.post("/reviews/r-need/dismiss", data={"page": "detail"})
    assert r.status_code == 200 and "closed" in r.text
    assert snap_mod.load(home, "r-need").phase == "done"


def test_raw_ticket_view_is_reviews_nav_and_links_page(web):
    c, _ = web
    r = c.get("/t/r-need")
    assert r.status_code == 200 and 'href="/reviews/r-need"' in r.text
    assert 'aria-current="page">Reviews' in r.text


def test_no_recorded_request(home):
    seed(home, "r-none", "awaiting-human", questions=["review-brief-r-none-x"], brief=True, request=False)
    with _client(home) as c:
        assert "not recorded" in c.get("/reviews/r-none").text
