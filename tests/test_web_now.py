"""Web Now page: `/` cards and ledger from view/now, live refresh wiring, `/tickets` table."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def _keys(html: str) -> set[str]:
    return set(re.findall(r'data-key="([^"]+)"', html))


@pytest.fixture
def client(seeded_home):
    with TestClient(create_app(seeded_home)) as c:
        yield c


def test_index_renders_cards_in_flight_and_queued(client):
    html = client.get("/").text
    cards = html.split('data-group="in-flight-agent"')[0]
    assert _keys(cards) >= {"T-1", "T-2"}
    assert '2 need <span class="you">you.</span>' in html
    assert 'data-key="T-4"' in html and 'data-key="T-5"' in html  # dispatcher-owned, queued
    assert 'data-slot="actions"' in html
    assert "Shall I proceed" in html


def test_each_group_refreshes_on_board_event(client):
    html = client.get("/").text
    for url in ("/now/headline", "/now/decide", "/now/ledger"):
        assert re.search(rf'hx-get="{url}"[^>]*hx-trigger="sse:board"', html)
        frag = client.get(url)
        assert frag.status_code == 200 and "<html" not in frag.text


def test_tickets_serves_filterable_table_and_rows(client):
    resp = client.get("/tickets")
    assert resp.status_code == 200 and "needs-you(2)" in resp.text
    assert 'hx-get="/board/rows?filter=needs-you"' in resp.text
    assert client.get("/tickets?filter=all").status_code == 200
    assert _keys(client.get("/board/rows?filter=all").text) >= {"T-1", "T-3"}


def test_headline_reads_differently_at_zero(home):
    seed_ticket(home, "T-9", "working", phase="implementing")
    with TestClient(create_app(home)) as c:
        html = c.get("/").text
    assert 'Nothing needs <span class="you">' in html and 'data-needs="0"' in html
    assert "Nothing is waiting on you" in html


def test_agent_text_is_escaped(home):
    seed_ticket(home, "T-9", "<script>alert(1)</script>", phase="awaiting-human",
                questions={"q1": "<img src=x onerror=1> ok?"})
    with TestClient(create_app(home)) as c:
        html = c.get("/").text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert '1 needs <span class="you">' in html
