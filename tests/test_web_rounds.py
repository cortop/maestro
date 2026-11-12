"""T-233: the web `/t/{key}/rounds` partial and its embedding in the ticket page."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import event_log, snapshot as snap_mod, steplog, store  # noqa: E402
from maestro.view import rounds as rounds_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

KEY = "T-950"


def _seed(home, title="Synthetic widget", reason="implementing round done"):
    store.atomic_write(store.spec_path(home, KEY), f"# {KEY}\n\n## Acceptance criteria\n- [ ] one\n")
    event_log.append(home, KEY, "TicketCreated", {"title": title, "source": "test", "spec_hash": "x"}, actor="t")
    event_log.append(home, KEY, "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, actor="t")
    event_log.append(home, KEY, "PhaseChanged", {"phase": "qa", "reason": reason}, actor="t")
    snap_mod.rebuild(home, KEY)


def test_rounds_partial_has_header_and_rows(home):
    _seed(home)
    with TestClient(create_app(home)) as c:
        r = c.get(f"/t/{KEY}/rounds")
    assert r.status_code == 200
    for label in ("Time", "Rounds", "QA", "Review", "PR", "Trouble"):
        assert f"<dt>{label}</dt>" in r.text
    assert 'class="round' in r.text
    assert "<td>QA</td>" in r.text


def test_rounds_partial_unknown_ticket_404(home):
    with TestClient(create_app(home)) as c:
        assert c.get("/t/T-999/rounds").status_code == 404


def test_ticket_page_embeds_partial_and_escapes(home):
    _seed(home, reason="<script>alert(1)</script> blocked")
    with TestClient(create_app(home)) as c:
        page = c.get(f"/t/{KEY}").text
        html = c.get(f"/t/{KEY}/rounds").text
    assert f'hx-get="/t/{KEY}/rounds"' in page
    assert 'id="rounds-header"' in html
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_repeated_refreshes_do_not_digest_sessions(home, monkeypatch):
    _seed(home)
    calls = []
    monkeypatch.setattr(steplog, "session_digest", lambda p: calls.append(p) or {})
    with TestClient(create_app(home)) as c:
        for _ in range(3):
            assert c.get(f"/t/{KEY}/rounds").status_code == 200
            assert c.get(f"/t/{KEY}").status_code == 200
    assert calls == []
    assert rounds_view.build(home, KEY).rows
