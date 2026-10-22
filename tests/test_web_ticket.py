"""Web ticket detail and the paged event timeline."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import event_log  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


@pytest.fixture
def client(home):
    seed_ticket(home, "T-7", "seven", phase="implementing", pr=7)
    for i in range(120):
        event_log.append(home, "T-7", "Note", {"text": f"n{i}"}, actor="t")
    (home / "tickets" / "T-7" / "proposal.md").write_text("# prop\nthe proposal", encoding="utf-8")
    with TestClient(create_app(home)) as c:
        yield c


def test_detail_has_ac_matrix_spec_proposal_and_dwell(client):
    html = client.get("/t/T-7").text
    assert 'id="ac-matrix"' in html and "<td>ok</td>" in html
    assert "## Intent" in html and "the proposal" in html
    assert "implementing" in html and "(now)" in html
    assert 'hx-trigger="sse:ticket:T-7"' in html


def test_event_pages_neither_skip_nor_duplicate(client, home):
    seen: list[int] = []
    url = "/t/T-7/events"
    while url:
        html = client.get(url).text
        seen += [int(s) for s in re.findall(r'data-seq="(\d+)"', html)]
        m = re.search(r'hx-get="(/t/T-7/events\?before=\d+)"', html)
        url = m.group(1) if m else None
    expected = [e["seq"] for e in event_log.read(home, "T-7")]
    assert seen == sorted(expected, reverse=True)
    assert len(seen) == len(set(seen)) > 100


def test_unknown_key_is_404(client):
    assert client.get("/t/T-404").status_code == 404
    assert client.get("/t/T-404/events").status_code == 404
