"""Web fleet page: the doctor rows are cached per process, refreshed off the request path."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import fleet, health  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture
def probe(monkeypatch):
    """Count `health.run_checks` calls and record the thread each ran on."""
    monkeypatch.setattr(fleet, "last_exit_code", lambda *a, **kw: None)
    calls: list[str] = []
    gate = threading.Event()
    gate.set()

    def run_checks(cfg, now, **kw):
        gate.wait(5)
        calls.append(threading.current_thread().name)
        return [{"name": "good", "status": "ok", "detail": "fine"}]

    monkeypatch.setattr(health, "run_checks", run_checks)
    run_checks.calls, run_checks.gate = calls, gate
    return run_checks


def test_get_does_not_run_doctor_in_request(home, probe):
    probe.gate.clear()  # the doctor run hangs; the request must still answer
    with TestClient(create_app(home)) as c:
        for path in ("/fleet", "/fleet/panels"):
            resp = c.get(path)
            assert resp.status_code == 200
            assert "doctor run in progress" in resp.text
            assert "doctor unavailable" not in resp.text
        assert probe.calls == []
        probe.gate.set()
        c.app.state.fleet_doctor.join()
        assert probe.calls and all(n == "fleet-doctor" for n in probe.calls)
        assert "checked" in c.get("/fleet/panels").text


def test_two_panel_reads_inside_ttl_run_doctor_once_and_post_reruns(home, probe):
    with TestClient(create_app(home)) as c:
        c.get("/fleet/panels")
        c.app.state.fleet_doctor.join()
        html = c.get("/fleet/panels").text
        assert len(probe.calls) == 1
        assert 'id="doctor-passing"' in html
        assert "checked 0s ago" in html  # the age of the displayed result
        resp = c.post("/fleet/doctor", headers={TOKEN_HEADER: c.app.state.guard.token})
        assert resp.status_code == 200, resp.text
        assert len(probe.calls) == 2


def test_stale_rows_served_while_refreshing(home, probe):
    with TestClient(create_app(home)) as c:
        cache = c.app.state.fleet_doctor
        c.get("/fleet/panels")
        cache.join()
        cache.ttl = 0  # everything is stale now
        probe.gate.clear()
        html = c.get("/fleet/panels").text
        assert 'id="doctor-passing"' in html and "refreshing" in html
        probe.gate.set()
        cache.join()
