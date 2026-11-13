"""T-252: Review card on the Now page, the Review tab, review status/rail, browser-only edits."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_review_ticket, seed_ticket  # noqa: E402
from maestro import event_log  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "maestro" / "web" / "static"


@pytest.fixture
def client(home):
    seed_review_ticket(home)
    with TestClient(create_app(home)) as c:
        yield c


def test_now_page_shows_review_card_linking_to_review_tab(client):
    html = client.get("/").text
    card = html.split('data-kind="review"')[1].split("</article>")[0]
    assert 'class="card review"' in html and "REVIEW" in card
    assert 'href="/t/T-60?tab=review"' in card
    assert "octo-reviewer" in card and "1 blocking" in card and "1 nit" in card
    assert "Shall I" not in card  # not rendered as a plain question card


def test_review_tab_link_opens_that_tab(client):
    html = client.get("/t/T-60?tab=review").text
    assert re.search(r'id="tab-review" class="tab-radio" checked', html)
    assert 'hx-get="/t/T-60/review"' in html
    assert "checked" not in html.split('id="tab-story"')[1].split(">")[0]
    plain = client.get("/t/T-60").text
    assert re.search(r'id="tab-story" class="tab-radio" checked', plain)


def test_review_tab_shows_brief_and_editable_findings(client):
    html = client.get("/t/T-60/review").text
    for text in ("What the PR does", "What was checked", "Decisions for you", "Reading order",
                 "request-changes", "Is the API ok?"):
        assert text in html
    assert html.count('class="finding"') == 2
    assert "src/a.py:3" in html and "Please guard the empty case." in html
    assert html.count('data-field="include"') == 2 and html.count('data-field="comment"') == 2
    assert '<option value="blocking" selected>' in html and '<option value="nit" selected>' in html
    assert "<form" not in html  # nothing here posts anywhere


def test_review_text_is_escaped(client):
    html = client.get("/t/T-60/review").text
    assert "<b>parser</b>" not in html and "&lt;b&gt;parser&lt;/b&gt;" in html


def test_status_sentence_and_rail_describe_a_review(client, home):
    html = client.get("/t/T-60").text
    assert "Brief ready" in html and "waiting on your submit" in html
    assert "brief ready" in html and "checking" in html and "implementing" not in html
    assert 'id="ac-matrix"' not in html
    seed_review_ticket(home, "T-61", brief=False, phase="qa")
    assert "checking the pull request" in client.get("/t/T-61").text


def test_non_review_ticket_has_no_review_tab_and_unchanged_status(home):
    seed_ticket(home, "T-9", "plain", phase="implementing")
    with TestClient(create_app(home)) as c:
        html = c.get("/t/T-9").text
    assert 'id="tab-review"' not in html and "implementing" in html


def test_editing_writes_nothing_server_side(client, home):
    before = event_log.read(home, "T-60")
    assert client.post("/t/T-60/review", data={"comment": "x"}).status_code in (404, 405)
    assert event_log.read(home, "T-60") == before
    routes = {r.path for r in client.app.routes}
    assert not any("github" in p or "submit" in p for p in routes)


def test_edits_persist_in_the_browser_across_reloads(client):
    js = (STATIC / "review.js").read_text()
    assert "localStorage.setItem" in js and "localStorage.getItem" in js
    assert 'addEventListener("htmx:afterSwap", restore)' in js  # a re-fetched panel is re-filled
    assert "fetch(" not in js and "XMLHttpRequest" not in js and "htmx.ajax" not in js
    assert 'src="/static/review.js"' in client.get("/t/T-60").text
    assert client.get("/static/review.js").status_code == 200
