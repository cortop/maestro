"""Neon Ledger web ticket page: status sentence, rail, evidence, dispatch/PR cards, tabs, escaping."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import event_log  # noqa: E402
from maestro.view import rounds as rounds_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


@pytest.fixture
def client(home):
    seed_ticket(home, "T-9", "nine <b>bold</b>", phase="implementing", pr=9)
    event_log.append(home, "T-9", "Note", {"text": "<script>alert(1)</script>"}, actor="t")
    (home / "tickets" / "T-9" / "proposal.md").write_text("prop <i>x</i>", encoding="utf-8")
    with TestClient(create_app(home)) as c:
        yield c


def test_page_renders_status_rail_evidence_dispatch_and_pr(client):
    html = client.get("/t/T-9").text
    for marker in ('id="status-sentence"', 'id="phase-rail"', 'id="rail-times"',
                   'id="ac-matrix"', 'id="dispatch-card"', 'id="pr-card"',
                   'id="ticket-actions"'):
        assert marker in html
    assert "implementing" in html and "T-9" in html
    assert "Criterion" in html and "Agent" in html and "QA" in html and "Check" in html
    assert "Verdict" in html and "Throttle" in html and "Depends on" in html
    assert "#9" in html


def test_tabs_sit_over_existing_fragments(client):
    html = client.get("/t/T-9").text
    for label, url in (("Story", "rounds"), ("Spec", "spec"), ("Proposal", "proposal"),
                       ("Events", "events"), ("Sessions", "logs")):
        assert f">{label}</label>" in html
        assert f'hx-get="/t/T-9/{url}"' in html
    assert "## Intent" in client.get("/t/T-9/spec").text
    assert "prop &lt;i&gt;x&lt;/i&gt;" in client.get("/t/T-9/proposal").text
    assert 'id="event-rows"' in client.get("/t/T-9/events").text
    assert client.get("/t/T-9/logs").status_code == 200
    assert client.get("/t/T-9/rounds").status_code == 200


def test_sections_refresh_on_ticket_events(client):
    html = client.get("/t/T-9").text
    assert html.count("sse:ticket:T-9") == 6  # the Sessions tab tails itself instead
    for url in ("head", "side"):
        assert f'hx-get="/t/T-9/{url}"' in html
        assert client.get(f"/t/T-9/{url}").status_code == 200


def test_agent_text_is_escaped(client):
    for url in ("/t/T-9", "/t/T-9/head", "/t/T-9/events", "/t/T-9/proposal"):
        html = client.get(url).text
        assert "<script>alert(1)" not in html and "<b>bold</b>" not in html
    assert "&lt;script&gt;" in client.get("/t/T-9/events").text
    assert "nine &lt;b&gt;bold&lt;/b&gt;" in client.get("/t/T-9/head").text


def test_unchanged_ticket_does_not_recompute_digests(client, home, tmp_path, monkeypatch):
    log = tmp_path / "s.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    calls = []
    from maestro import steplog
    monkeypatch.setattr(steplog, "session_digest",
                        lambda p: calls.append(p) or {"supported": False, "note": "n"})
    rounds_view._DIGEST_CACHE.clear()
    rounds_view.cached_digest(log)
    rounds_view.cached_digest(log)
    assert len(calls) == 1
    client.get("/t/T-9")  # the page itself never digests sessions
    assert len(calls) == 1
