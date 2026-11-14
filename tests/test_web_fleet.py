"""Web fleet page: `GET /fleet` panels, live-refresh wiring and "unknown" degradation."""
from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_phase  # noqa: E402
from maestro import claims, fleet, health, spend, store  # noqa: E402
from maestro.statemachine import Phase  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    """The doctor's launchctl check must never shell out."""
    monkeypatch.setattr(fleet, "last_exit_code", lambda *a, **kw: None)


def _heartbeat(home):
    store.write_json(store.heartbeat_path(home),
                     {"ts": "2026-01-01T00:00:00+00:00", "epoch": store.now_epoch(),
                      "spawned": 0, "active": 1, "throttled": 0, "due": 0, "paused": False})


def _get(home, path="/fleet"):
    with TestClient(create_app(home)) as c:
        c.get(path)  # starts the off-request doctor run
        c.app.state.fleet_doctor.join()
        return c.get(path)


def test_fleet_renders_every_panel(home, monkeypatch):
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "r")
    _heartbeat(home)
    monkeypatch.setattr(health, "run_checks",
                        lambda cfg, now: [{"name": "good", "status": "ok", "detail": "fine"}])
    cfg_toml = (home / "config.toml")
    cfg_toml.write_text("[maestro]\ndaily_spend_ceiling_usd = 10.0\n"
                        "[alarm]\nspend_warn_fractions = [0.5, 0.8]\n"
                        "[[scheduled]]\nname = \"nightly\"\nevery = \"1d\"\nprompt = \"x\"\n")
    store.write_json(spend._state_path(home),
                     {"date": store.utc_date(store.now_epoch()), "total_usd": 4.0})
    resp = _get(home)
    assert resp.status_code == 200
    html = resp.text
    assert re.search(r'id="fleet-state">healthy<', html)
    assert "$4.00 / $10.00" in html
    assert 'style="left: 50.0%"' in html and 'style="left: 80.0%"' in html
    assert 'style="width: 40.0%"' in html
    assert 'id="panel-spawns"' in html and 'class="buckets"' in html
    assert "clear" in html  # rate-limit state
    assert 'data-key="T-1"' in html  # live sessions
    assert "nightly" in html  # schedule
    assert 'id="doctor-passing"' in html and 'id="backups"' in html
    assert 'id="fleet-actions"' in html  # T-244 slot


def test_doctor_problems_listed_before_collapsed_passing(home, monkeypatch):
    _heartbeat(home)
    rows = [{"name": "good", "status": "ok", "detail": "fine"},
            {"name": "bad", "status": "fail", "detail": "broken"}]
    monkeypatch.setattr(health, "run_checks", lambda cfg, now: rows)
    html = _get(home).text
    assert html.index("broken") < html.index('id="doctor-passing"') < html.index("fine")
    assert "needs attention" in html and "1 doctor check(s) need attention" in html


def test_panels_refresh_on_fleet_event(home):
    html = _get(home).text
    m = re.search(r'<div id="fleet"[^>]*>', html)
    assert m and 'hx-trigger="sse:fleet"' in m.group(0) and 'hx-get="/fleet/panels"' in m.group(0)
    assert 'sse-connect="/stream"' in html
    frag = _get(home, "/fleet/panels")
    assert frag.status_code == 200 and "<html" not in frag.text and 'id="fleet-state"' in frag.text


def test_missing_heartbeat_is_unknown_not_500(home):
    resp = _get(home)
    assert resp.status_code == 200
    assert re.search(r'id="fleet-state">unknown<', resp.text)
    assert "heartbeat unknown" in resp.text


def test_unreadable_spend_meter_is_unknown(home):
    _heartbeat(home)
    spend._state_path(home).parent.mkdir(parents=True, exist_ok=True)
    spend._state_path(home).write_text("{not json")
    resp = _get(home)
    assert resp.status_code == 200
    assert "spend meter unknown" in resp.text
    panel = resp.text.split('id="panel-spend"')[1].split('id="panel-spawns"')[0]
    assert "unknown" in panel and "$" not in panel


def test_view_failure_degrades_panel_not_page(home, monkeypatch):
    from maestro.view import fleet as fv
    _heartbeat(home)
    monkeypatch.setattr(fv, "schedule_rows", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(fv, "backup_panel", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    resp = _get(home)
    assert resp.status_code == 200 and resp.text.count(">unknown<") >= 2
