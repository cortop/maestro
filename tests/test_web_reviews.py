"""Web Reviews page: cards and rows with request fields, nav badge, filters, guarded actions, empty states."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_origin_and_repo  # noqa: E402
from maestro import event_log, review, store  # noqa: E402
from maestro import snapshot as snap_mod  # noqa: E402
from maestro.web import routes_reviews  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402
from test_review_kind import URL, FakeVCS, _push_pr_head  # noqa: E402
from test_view_reviews import seed  # noqa: E402

NAV_IDS = ("Now", "Tickets", "Reviews", "Activity", "Fleet")


def _app(home, **kw):
    app = create_app(home, nudge_on_action=False, **kw)
    return app, TestClient(app)


@pytest.fixture
def web(home):
    seed(home, "r-need", "awaiting-human", questions=["review-brief-r-need-x"], brief=True, number=1)
    seed(home, "r-prog", "qa", number=2)
    seed(home, "r-gh", "awaiting-ci", brief=True, pending=True, number=3)
    seed(home, "r-done", "done", brief=True, reason="review submitted", number=4)
    path = store.snapshot_path(home, "r-done")  # closed yesterday, relative to the real clock
    d = store.read_json(path)
    d["updated_ts"] = datetime.fromtimestamp(store.now_epoch() - 86400, timezone.utc).isoformat()
    store.atomic_write(path, json.dumps(d))
    for key, n in (("r-need", 1), ("r-prog", 2)):  # the author rides on the round-started event
        event_log.append(home, key, "ReviewRoundStarted", {"round": 1, "head_sha": "a" * 40, "author": "bob",
                                                           "url": f"https://github.com/acme/widgets/pull/{n}"}, actor="h")
        event_log.append(home, key, "ReviewFindingRecorded", {"head_sha": "a" * 40, "file": "a.py", "line": 1,
                                                              "severity": "nit", "comment": "c"}, actor="r")
        snap_mod.rebuild(home, key)
    app, c = _app(home)
    with c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home


def test_card_and_row_carry_request_fields(web):
    c, _ = web
    html = c.get("/reviews").text
    card = re.search(r'<article class="card review".*?</article>', html, re.S).group(0)
    for want in ("Fix parser", "acme/widgets #1", "by bob", "asked by <b>Ada</b>", "2026-05-31 10:00",
                 'href="https://x/p1"', "1 nit", 'href="/reviews/r-need"'):
        assert want in card, want
    table = re.search(r'<table id="reviews".*?</table>', html, re.S).group(0)
    assert 'data-key="r-prog"' in table and "<b>Ada</b>" in table and 'href="https://x/p1"' in table
    assert "Fix parser" in table and "acme/widgets #2" in table and "bob" in table
    for cls in ("review-cards", "review-ask", "review-start", "review-fresh"):
        assert cls in html
    css = (routes_reviews.__file__.rsplit("/", 1)[0] + "/static/maestro.css")
    css = open(css).read()
    assert css.count("/* reviews page */") == 1


def test_reviews_block_adds_no_colour_or_font(web):
    block = open(routes_reviews.__file__.rsplit("/", 1)[0] + "/static/maestro.css").read().split("/* reviews page */")[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb|hsl", block)
    assert "font-family" not in block and not re.search(r"font:[^;}]*\"", block)


def test_nav_badge_and_fragment(web):
    c, _ = web
    for path in ("/reviews", "/", "/activity"):
        assert re.search(r'<a href="/reviews" data-count="1" data-alert data-sub="1 needs you"', c.get(path).text)
    frag = c.get("/shell/nav", params={"current": "reviews"}).text
    assert re.search(r'<a href="/reviews"[^>]*class="current" aria-current="page">Reviews', frag)
    for label in NAV_IDS:
        assert frag.count(f">{label}</a>") == 1
    assert 'hx-get="/shell/nav?current=reviews"' in frag and 'hx-trigger="sse:board"' in frag
    assert frag.count("<nav") == 1


def test_filters_partition_and_history(web):
    c, _ = web
    html = c.get("/reviews").text
    for label, n in (("open", 3), ("needs you", 1), ("in progress", 1), ("on github", 1), ("history", 1)):
        assert f">{label}({n})</a>" in html
    keys = lambda h: set(re.findall(r'data-key="(r-[a-z]+)"', h))  # noqa: E731
    assert keys(html) == {"r-need", "r-prog", "r-gh"}
    parts = [keys(c.get("/reviews", params={"filter": f}).text) for f in ("needs", "progress", "github")]
    assert parts == [{"r-need"}, {"r-prog"}, {"r-gh"}]
    hist = c.get("/reviews", params={"filter": "history"}).text
    assert keys(hist) == {"r-done"} and "submitted" in hist and "Recently closed" in hist


def test_actions_start_and_dismiss_and_token(home, tmp_path):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(f'[maestro]\n[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    sha = _push_pr_head(origin, repo, tmp_path, "pr-one")
    app, c = _app(home, vcs=FakeVCS(sha))
    with c:
        assert c.post("/reviews/start", data={"url": URL}).status_code == 403
        c.headers[TOKEN_HEADER] = app.state.guard.token
        r = c.post("/reviews/start", data={"url": URL})
        assert r.status_code == 200
        key = "review-acme-widgets-7"
        assert [q["via"] for q in review.load(home, key).requests] == ["web"]
        assert c.post("/reviews/start", data={"url": "https://example.com/nope"}).status_code == 409
        assert c.post(f"/reviews/{key}/dismiss", headers={TOKEN_HEADER: ""}).status_code == 403
        assert c.post(f"/reviews/{key}/dismiss", data={}).status_code == 200
        assert snap_mod.load(home, key).phase == "done"
        assert c.post("/reviews/nope/retry", data={}).status_code == 409


def test_read_only_has_no_controls_or_routes(home):
    seed(home, "r-need", "awaiting-human", questions=["review-brief-r-need-x"], brief=True)
    app, c = _app(home, read_only=True)
    with c:
        html = c.get("/reviews").text
        assert "review-start" not in html.replace("review-start{", "") and "Mark reviewed" not in html
        assert 'href="/reviews/r-need"' in html
        for path in ("/reviews/start", "/reviews/r-need/dismiss", "/reviews/r-need/retry"):
            assert c.post(path, data={}).status_code in (404, 405)


def test_not_recorded_and_escaping(home):
    seed(home, "r-x", "awaiting-human", questions=["review-brief-r-x-q"], brief=True, request=False)
    seed(home, "r-y", "qa", number=9)
    store.atomic_write(store.spec_path(home, "r-z"), "# r-z\n")
    event_log.append(home, "r-y", "ReviewPrObserved", {"title": "<script>alert(1)</script>", "author": "<b>eve</b>",
                                                       "head_sha": "a" * 40}, actor="d")
    snap_mod.rebuild(home, "r-y")
    app, c = _app(home)
    with c:
        html = c.get("/reviews").text
    assert "not recorded" in html and "noticed" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_empty_states(home):
    app, c = _app(home)
    with c:
        html = c.get("/reviews").text
    assert "No review requests. Mention you with a PR link in the watched channel, or paste a PR URL above." in html
    assert "Slack watcher is off" in html


def test_templates_have_no_literal_host():
    root = routes_reviews.__file__.rsplit("/", 1)[0]
    for f in ("templates/reviews.html", "templates/_reviews_rows.html", "static/maestro.css"):
        assert not re.search(r"https?://|\.com\b", open(f"{root}/{f}").read()), f


def test_palette_goto_entry_and_order(home):
    app, c = _app(home)
    with c:
        html = c.get("/palette").text
    assert "Open reviews" in html
    groups = re.findall(r'<li class="palette-group" role="presentation">(.*?)</li>', html)
    assert groups == ["Go to", "Fleet", "Tickets"] or groups[:2] == ["Go to", "Fleet"]


def test_when_formats():
    now = datetime(2026, 6, 1, 12, tzinfo=timezone.utc).timestamp()
    assert routes_reviews.when("2026-06-01T11:48:00+00:00", now) == "12 min ago"
    assert routes_reviews.when(None, now) == ""
