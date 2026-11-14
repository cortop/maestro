"""Web palette ordering: commands precede tickets, Go to group, keyword aliases, uncapped open tickets."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro.view import board as board_view  # noqa: E402
from maestro.web import routes_palette  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture
def web(home):
    seed_ticket(home, "T-9", "Nine", phase="implementing")
    seed_ticket(home, "T-10", "Ten", phase="implementing")
    seed_ticket(home, "T-3", "Old", phase="done")
    app = create_app(home, nudge_on_action=False)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home


def _groups(html):
    return re.findall(r'<li class="palette-group" role="presentation">(.*?)</li>', html)


def _entries(html):
    return re.findall(r'<li class="palette-entry"[^>]*?data-text="(.*?)"[^>]*>\s*<span class="palette-label">(.*?)</span>', html)


def test_group_order_and_commands_before_tickets(web):
    c, _ = web
    html = c.get("/palette", params={"key": "T-9"}).text
    assert _groups(html) == ["T-9 actions", "Go to", "Fleet", "Tickets"]
    assert _groups(c.get("/palette").text) == ["Go to", "Fleet", "Tickets"]
    labels = [l for _, l in _entries(html)]
    first = next(i for i, l in enumerate(labels) if re.match(r"T-\d+ ", l) and "actions" not in l
                 and l.split()[0] in ("T-9", "T-10", "T-3") and l.split(" ", 1)[1] in ("Nine", "Ten", "Old"))
    assert labels.index("Back up now") < first and labels.index("Hold T-9") < first
    assert 'data-href="/t/T-10"' not in html.split("Tickets</li>")[0]


def test_go_to_group_entries(web):
    c, _ = web
    html = c.get("/palette").text
    go = html.split("Fleet</li>")[0]
    assert 'data-href="/"' in go and 'data-href="/tickets"' in go
    for name in board_view.FILTER_NAMES:
        assert f'data-href="/tickets?filter={name}"' in go


def test_aliases_in_data_text_labels_unchanged(web):
    c, _ = web
    ent = dict((l, t) for t, l in _entries(c.get("/palette").text))
    assert "backup" in ent["Back up now"] and "health" in ent["Re-run doctor"]


def test_open_tickets_survive_cap_and_natural_order(web, monkeypatch):
    c, home = web
    monkeypatch.setattr(routes_palette, "MAX_TICKETS", 2)
    for i in range(20, 24):
        seed_ticket(home, f"T-{i}", "x", phase="done")
    html = c.get("/palette").text
    labels = [l for _, l in _entries(html) if re.match(r"T-\d+ ", l)]
    assert labels[:2] == ["T-10 Ten", "T-9 Nine"]  # non-terminal first, natural desc
    assert len(labels) == 2  # cap hit by open tickets; terminal dropped
