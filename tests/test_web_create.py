"""Web UI create / review / import-from-Linear: the real app over a temp home; only `gh` and Linear are faked."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_origin_and_repo  # noqa: E402
from maestro import dispatcher as disp  # noqa: E402
from maestro import snapshot as snap_mod  # noqa: E402
from maestro.config import Config  # noqa: E402
from maestro.providers.linear import LinearTracker  # noqa: E402
from maestro.sessions import DryRunSessions  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402
from test_linear_import import FakeLinearTransport, _issue  # noqa: E402
from test_review_kind import URL, FakeVCS, _push_pr_head  # noqa: E402

REVIEW_KEY = "review-acme-widgets-7"


def _client(home, **kw):
    app = create_app(home, nudge_on_action=False, **kw)
    return app, TestClient(app)


@pytest.fixture
def web(home):
    transport = FakeLinearTransport([_issue("ENG-42", "Fix the widget")])
    app, c = _client(home, tracker=LinearTracker({}, transport=transport))
    with c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home, transport


def test_create_queues_ticket_like_the_cli(web):
    c, home, _ = web
    r = c.post("/new/create", data={"title": "Ship the thing", "priority": "1"})
    assert r.status_code == 200 and "Ship the thing" in r.text
    report = disp.dispatch(Config(home=home), DryRunSessions(), now=1000)
    snaps = [snap_mod.load(home, k) for k in report.minted]
    assert [(s.title, ) for s in snaps] == [("Ship the thing",)]
    assert "priority: 1" in (home / "tickets" / report.minted[0] / "spec.md").read_text()


def test_create_empty_title_refused_inline(web):
    c, home, _ = web
    r = c.post("/new/create", data={"title": "   ", "priority": "1"})
    assert r.status_code == 409 and "title must not be empty" in r.text
    assert not list((home / "inbox").glob("_new*"))
    assert disp.dispatch(Config(home=home), DryRunSessions(), now=1000).minted == []


def test_review_links_to_ticket_and_inline_refusals(home, tmp_path, monkeypatch):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        f'[maestro]\n[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    sha = _push_pr_head(origin, repo, tmp_path, "pr-one")
    app, c = _client(home, vcs=FakeVCS(sha))
    with c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        r = c.post("/new/review", data={"url": URL})
        assert r.status_code == 200 and f'href="/t/{REVIEW_KEY}"' in r.text
        assert snap_mod.load(home, REVIEW_KEY).kind == "review"
        r = c.post("/new/review", data={"url": URL})
        assert r.status_code == 200 and "no new review round" in r.text
        r = c.post("/new/review", data={"url": "https://example.com/nope"})
        assert r.status_code == 409 and "not a GitHub pull request URL" in r.text


def test_import_linear_and_reimport_is_noop(web):
    c, home, transport = web
    r = c.post("/new/import", data={"ref": "https://linear.app/acme/issue/ENG-42/fix"})
    assert r.status_code == 200 and 'href="/t/LINEAR-ENG-42"' in r.text
    assert snap_mod.load(home, "LINEAR-ENG-42").title
    calls = len(transport.calls)
    r = c.post("/new/import", data={"ref": "ENG-42"})
    assert r.status_code == 200 and "already imported" in r.text
    assert len(transport.calls) == calls
    assert c.post("/new/import", data={"ref": "garbage"}).status_code == 409


def test_read_only_hides_controls_and_refuses_posts(home):
    app, c = _client(home, read_only=True)
    with c:
        page = c.get("/new").text
        assert "<form" not in page and "Writes are disabled" in page
        for path, data in (("/new/create", {"title": "x"}), ("/new/review", {"url": URL}),
                           ("/new/import", {"ref": "ENG-1"})):
            assert c.post(path, data=data).status_code in (403, 404, 405)
        assert "New ticket" not in c.get("/palette").text
    assert not list((home / "inbox").glob("_new*"))


def test_palette_lists_the_three_actions(web):
    c, _, _ = web
    body = c.get("/palette").text
    for label in ("New ticket", "Review a PR", "Import from Linear"):
        assert label in body
    page = c.get("/new").text
    assert 'hx-post="/new/create"' in page and 'hx-post="/new/review"' in page \
        and 'hx-post="/new/import"' in page
