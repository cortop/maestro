"""Web activity page: cross-ticket feed, category and text filters, live-update and pause wiring."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import event_log  # noqa: E402
from maestro.view import activity as activity_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


@pytest.fixture
def web(home):
    seed_ticket(home, "T-1", "Alpha widget", phase="implementing")
    seed_ticket(home, "T-2", "Beta gadget", phase="qa")
    event_log.append(home, "T-1", "Note", {"text": "zebra stripes"}, actor="r")
    with TestClient(create_app(home, nudge_on_action=False)) as c:
        yield c, home


def _rows(html):
    return re.findall(r'<tr class="event" data-key="([^"]+)" data-seq="(\d+)" data-cat="(\d)">', html)


def test_feed_lists_events_across_tickets_newest_first_and_links(web):
    c, home = web
    html = c.get("/activity").text
    rows = _rows(html)
    assert {k for k, _, _ in rows} == {"T-1", "T-2"}
    assert len(rows) == 6  # every event of both tickets
    assert 'href="/t/T-1"' in html and "zebra stripes" in html
    assert "PhaseChanged" in html and "<th scope=\"col\">Time</th>" in html
    stamps = re.findall(r'<td class="ts">([^<]*)</td>', html)
    assert stamps == sorted(stamps, reverse=True)
    assert 'href="/activity"' in c.get("/").text  # nav entry


def test_palette_has_activity_entry(web):
    c, _ = web
    assert "Open activity" in c.get("/palette").text


def test_category_toggles_and_text_filter_narrow_the_feed(web):
    c, _ = web
    work = _rows(c.get("/activity", params={"cat": 5}).text)
    assert work and {cat for _, _, cat in work} == {"5"}
    life = _rows(c.get("/activity/rows", params={"cat": [1]}).text)
    assert life and all(cat == "1" for _, _, cat in life)
    page = c.get("/activity", params={"cat": 5}).text
    assert re.search(r'data-cat="5" class="active"', page)
    assert re.search(r'data-cat="1" aria-pressed="false"', page)
    hit = _rows(c.get("/activity", params={"q": "zebra"}).text)
    assert [k for k, _, _ in hit] == ["T-1"] and len(hit) == 1
    by_key = {k for k, _, _ in _rows(c.get("/activity", params={"q": "t-2"}).text)}
    assert by_key == {"T-2"}
    assert "no events match" in c.get("/activity", params={"q": "nothing-like-this"}).text


def test_live_update_and_pause_wiring(web):
    c, home = web
    html = c.get("/activity", params={"cat": 5, "q": "z"}).text
    # the feed re-fetches itself on every board change, keeping the active filters ...
    assert re.search(r'hx-get="/activity/rows\?cat=5&amp;q=z" hx-trigger="sse:board\[!window\.maestroActivityPaused\]"', html)
    assert 'id="activity-pause"' in html and "/static/activity.js" in html
    # ... and a new event shows up in the fragment without a page reload
    before = len(_rows(c.get("/activity/rows").text))
    event_log.append(home, "T-2", "Note", {"text": "fresh arrival"}, actor="r")
    frag = c.get("/activity/rows").text
    assert len(_rows(frag)) == before + 1 and "fresh arrival" in frag
    js = c.get("/static/activity.js").text
    assert "maestroActivityPaused = !window.maestroActivityPaused" in js and "htmx.trigger" in js


def test_read_only_and_bounded_window(home):
    seed_ticket(home, "T-1", "Alpha", phase="implementing")
    for i in range(300):
        event_log.append(home, "T-1", "Note", {"text": f"n{i}" + "x" * 400}, actor="r")
    rows = activity_view.recent(home, per_key=10)
    assert len(rows) == 10 and rows[0]["summary"].startswith("n299")
    with TestClient(create_app(home, read_only=True)) as c:
        assert c.get("/activity").status_code == 200
