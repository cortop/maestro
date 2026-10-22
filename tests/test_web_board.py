"""Web board: full page, filter fragments, needs-you counts."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import claims  # noqa: E402
from maestro.config import Config  # noqa: E402
from maestro.view import board as board_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def _keys(html: str) -> set[str]:
    return set(re.findall(r'data-key="([^"]+)"', html))


@pytest.fixture
def client(seeded_home):
    with TestClient(create_app(seeded_home)) as c:
        yield c


@pytest.mark.parametrize("name", ["needs-you", "active", "all"])
def test_rows_fragment_matches_view_layer(client, seeded_home, name):
    expected = {r.key for r in board_view.board(seeded_home, Config(home=seeded_home), name).rows}
    resp = client.get(f"/board/rows?filter={name}")
    assert resp.status_code == 200 and "<html" not in resp.text
    assert _keys(resp.text) == expected and expected


def test_index_renders_board_with_needs_you_count(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "needs you: 2" in resp.text and "needs-you(2)" in resp.text
    assert _keys(resp.text) == {"T-1", "T-2"}
    assert 'sse-connect="/stream"' in resp.text


def test_unknown_filter_is_400(client):
    assert client.get("/board/rows?filter=nope").status_code == 400


def test_routes_read_claims_via_describe_claims(seeded_home, monkeypatch):
    calls = []
    real = claims.describe_claims
    monkeypatch.setattr(claims, "describe_claims", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    for forbidden in ("active_keys", "is_claimed"):
        monkeypatch.setattr(claims, forbidden, lambda *a, **kw: pytest.fail(f"{forbidden} used"))
    with TestClient(create_app(seeded_home)) as c:
        assert c.get("/board/rows?filter=all").status_code == 200
    assert calls
