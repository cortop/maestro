"""Web fleet actions: guarded pause / resume / doctor / backup / stop routes over a temp home."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_phase  # noqa: E402
from maestro import backup, claims, fleet, human_actions, ops  # noqa: E402
from maestro.config import load  # noqa: E402
from maestro.statemachine import Phase  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    monkeypatch.setattr(fleet, "last_exit_code", lambda *a, **kw: None)


def _post(home, path, data=None, token=True, **kw):
    app = create_app(home, **kw)
    with TestClient(app) as c:
        h = {TOKEN_HEADER: app.state.guard.token} if token else {}
        return c.post(path, data=data or {}, headers=h)


def test_pause_with_reason_and_resume(home):
    r = _post(home, "/fleet/pause", {"until": "", "reason": "lunch"})
    assert r.status_code == 200 and 'id="fleet-state"' in r.text and ">paused<" in r.text
    assert fleet.pause_state(home, 0)["reason"] == "lunch"
    r = _post(home, "/fleet/resume")
    assert r.status_code == 200 and fleet.pause_state(home, 0) is None


def test_pause_until_and_bad_until(home):
    _post(home, "/fleet/pause", {"until": "4102444800"})
    assert fleet.pause_state(home, 0)["until"] == 4102444800.0
    fleet.resume(home)
    r = _post(home, "/fleet/pause", {"until": "garbage"})
    assert "invalid until" in r.text and fleet.pause_state(home, 0) is None


def test_doctor_and_backup_return_panels(home):
    assert 'id="doctor-' in _post(home, "/fleet/doctor").text
    assert backup.list_backups(load(str(home))) == []
    r = _post(home, "/fleet/backup")
    assert "backup created" in r.text
    assert len(backup.list_backups(load(str(home)))) == 1


def test_routes_are_guarded(home):
    for p in ("/fleet/pause", "/fleet/resume", "/fleet/doctor", "/fleet/backup", "/fleet/stop/T-1"):
        assert _post(home, p, token=False).status_code == 403
    assert fleet.pause_state(home, 0) is None
    assert _post(home, "/fleet/pause", read_only=True).status_code in (404, 405)


def test_stop_needs_confirm_and_uses_shared_module(home, monkeypatch):
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "r")
    calls = []
    monkeypatch.setattr(human_actions, "stop", lambda cfg, key, **kw: calls.append(key) or {"stopped": True})
    app = create_app(home)
    with TestClient(app) as c:
        assert "Confirm stop" in c.get("/fleet/stop/T-1").text
        h = {TOKEN_HEADER: app.state.guard.token}
        assert "not confirmed" in c.post("/fleet/stop/T-1", headers=h).text and calls == []
        assert "stopped T-1" in c.post("/fleet/stop/T-1", data={"confirm": "yes"}, headers=h).text
    assert calls == ["T-1"]


def test_stop_refusal_is_reported(home, monkeypatch):
    monkeypatch.setattr(ops, "stop_session", lambda cfg, key, wait=10.0: {"stopped": False, "outcome": "no live session"})
    r = _post(home, "/fleet/stop/T-9", {"confirm": "yes"})
    assert "refused" in r.text and "no live session" in r.text


def test_restore_has_no_route_and_command_shown(home):
    app = create_app(home)
    assert not any("restore" in getattr(r, "path", "") for r in app.routes)
    with TestClient(app) as c:
        assert "maestro restore" in c.get("/fleet").text
