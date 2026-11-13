"""Web command palette: the server-rendered /palette fragment and the vanilla script serving it."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import inbox  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture
def web(home):
    seed_ticket(home, "T-1", "Alpha widget", phase="implementing")
    seed_ticket(home, "T-2", "Broken thing", phase="degraded")
    seed_ticket(home, "T-3", "Finished work", phase="done")
    app = create_app(home, nudge_on_action=False)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home


def _labels(html):
    return re.findall(r'<span class="palette-label">(.*?)</span>', html)


def test_palette_lists_ticket_goto_and_fleet_groups(web):
    c, _ = web
    html = c.get("/palette", params={"key": "T-1"}).text
    for g in ("T-1 actions", "Go to", "Fleet"):
        assert g in html
    labels = _labels(html)
    assert "Hold T-1" in labels and "Message agent on T-1" in labels
    assert any(l.startswith("T-2 Broken thing") for l in labels)  # tickets match by key and title
    assert "Pause fleet" in labels and "Back up now" in labels


def test_invalid_actions_absent_and_tui_keys_shown(web):
    c, _ = web
    working = c.get("/palette", params={"key": "T-1"}).text
    assert "Retry T-1" not in working and "Release T-1" not in working
    assert "<kbd>h</kbd>" in working and "<kbd>P</kbd>" in working
    degraded = c.get("/palette", params={"key": "T-2"}).text
    assert 'data-post="/t/T-2/retry"' in degraded and "<kbd>ctrl+r</kbd>" in degraded
    done = _labels(c.get("/palette", params={"key": "T-3"}).text)
    assert not any(l.endswith("T-3") and l.split()[0] in ("Hold", "Retry", "Message") for l in done)


def test_no_ticket_group_off_ticket_page_and_unknown_key(web):
    c, _ = web
    for params in ({}, {"key": "T-999"}):
        html = c.get("/palette", params=params).text
        assert " actions" not in html and "Go to needs you" in html


def test_read_only_hides_write_entries(home):
    seed_ticket(home, "T-1", "Alpha", phase="implementing")
    with TestClient(create_app(home, read_only=True, nudge_on_action=False)) as c:
        html = c.get("/palette", params={"key": "T-1"}).text
    assert "data-post" not in html and "Open fleet" in html


def test_palette_post_entry_runs_real_route(web):
    c, home = web
    entry = re.search(r'data-post="(/t/T-1/hold)"', c.get("/palette", params={"key": "T-1"}).text)
    assert c.post(entry.group(1)).status_code == 200
    html = c.get("/palette", params={"key": "T-1"}).text
    assert "Release T-1" in html and "Hold T-1" not in html


def test_pages_wire_header_control_script_and_ticket_key(web):
    c, _ = web
    page = c.get("/t/T-1").text
    assert "data-palette-open" in page and "Command" in page
    assert 'data-ticket="T-1"' in page and "/static/palette.js" in page
    assert "data-ticket" not in c.get("/fleet").text


def test_script_is_static_vanilla_with_keys(web):
    c, _ = web
    r = c.get("/static/palette.js")
    assert r.status_code == 200
    js = r.text
    for needle in ("metaKey", "ctrlKey", "ArrowDown", "ArrowUp", "Enter", "Escape", "/palette"):
        assert needle in js
    assert "import " not in js and "require(" not in js
