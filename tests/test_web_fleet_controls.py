"""Web fleet controls: up/down, sweep, dry run, rate limit, claims purge and the backups list."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_phase  # noqa: E402
from maestro import backup, claims, fleet, ratelimit, store  # noqa: E402
from maestro.config import load  # noqa: E402
from maestro.sessions import DryRunSessions  # noqa: E402
from maestro.statemachine import Phase  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    monkeypatch.setattr(fleet, "last_exit_code", lambda *a, **kw: None)
    state = {"loaded": False, "calls": []}

    def up(home, interval=300, **kw):
        state["loaded"] = True
        state["calls"].append("up")
        return {"action": "up", "rc": 0, "stdout": "loaded", "label": "x"}

    def down(home, **kw):
        state["loaded"] = False
        state["calls"].append("down")
        return {"action": "down", "rc": 0, "stdout": "", "label": "x"}

    monkeypatch.setattr(fleet, "up", up)
    monkeypatch.setattr(fleet, "down", down)
    monkeypatch.setattr(fleet, "status", lambda home, **kw: {"loaded": state["loaded"]})
    return state


def _post(home, path, data=None, token=True, **kw):
    app = create_app(home, **kw)
    with TestClient(app) as c:
        h = {TOKEN_HEADER: app.state.guard.token} if token else {}
        return c.post(path, data=data or {}, headers=h)


def test_fleet_up_and_down_need_confirm(home, no_launchctl):
    assert "not confirmed" in _post(home, "/fleet/up").text
    assert no_launchctl["calls"] == []
    r = _post(home, "/fleet/up", {"confirm": "yes"})
    assert "fleet up: exit 0" in r.text and 'id="launchd-state">loaded<' in r.text
    r = _post(home, "/fleet/down", {"confirm": "yes"})
    assert "fleet down: exit 0" in r.text and ">not loaded<" in r.text
    assert no_launchctl["calls"] == ["up", "down"]


def test_sweep_now_reports_spawns_and_dry_run_writes_nothing(home):
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    sessions = DryRunSessions()
    r = _post(home, "/fleet/sweep", sessions=sessions)
    assert r.status_code == 200 and "sweep: spawned T-1" in r.text
    assert [s[0] for s in sessions.spawned] == ["T-1"]

    before = (home / "events" / "T-1.jsonl").read_text()
    probe = DryRunSessions()
    r = _post(home, "/fleet/dry-run", sessions=probe)
    assert "dry run: would mint nothing; would spawn" in r.text
    assert probe.spawned == []
    assert (home / "events" / "T-1.jsonl").read_text() == before


def test_dry_run_lists_would_mint(home):
    (home / "inbox").mkdir(exist_ok=True)
    r = _post(home, "/fleet/dry-run")
    assert r.status_code == 200 and "dry run:" in r.text
    assert not list((home / "events").glob("*.jsonl"))


def test_clear_rate_limit_only_while_limited(home):
    app = create_app(home)
    with TestClient(app) as c:
        h = {TOKEN_HEADER: app.state.guard.token}
        assert "clear-rate-limit" not in c.get("/fleet").text
        assert "not rate-limited" in c.post("/fleet/clear-rate-limit", headers=h).text
        store.write_json(ratelimit._state_path(home), {"paused_until": store.now_epoch() + 600})
        assert ratelimit.paused_until(home, store.now_epoch()) is not None
        assert 'id="clear-rate-limit"' in c.get("/fleet").text
        assert "rate limit cleared" in c.post("/fleet/clear-rate-limit", headers=h).text
        assert ratelimit.paused_until(home, store.now_epoch()) is None
        assert "clear-rate-limit" not in c.get("/fleet").text


def test_claims_list_and_purge_stale(home):
    seed_phase(home, "T-1", Phase.IMPLEMENTING)
    seed_phase(home, "T-2", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", os.getpid(), "r")
    claims.write_claim(home, "T-2", 2**22 + 12345, "r")  # no such process
    cfg = load(str(home))
    rows = {r["key"]: r for r in claims.describe_claims(home, max_age=cfg.unverified_claim_max_age)}
    assert rows["T-1"]["claimed"] and not rows["T-2"]["claimed"]
    app = create_app(home)
    with TestClient(app) as c:
        page = c.get("/fleet").text
        assert 'id="claims"' in page and 'data-key="T-1"' in page and 'data-key="T-2"' in page
        assert f"pid {os.getpid()}" in page and "purge-claims" in page
        r = c.post("/fleet/purge-claims", headers={TOKEN_HEADER: app.state.guard.token})
    assert "T-2" in r.text and "T-1," not in r.text
    assert claims.read_claim(home, "T-2") is None
    assert claims.read_claim(home, "T-1") is not None


def test_backups_listed_without_restore(home):
    cfg = load(str(home))
    path = backup.create_backup(cfg, store.now_epoch())
    with TestClient(create_app(home)) as c:
        page = c.get("/fleet").text
    assert 'id="backup-files"' in page and path.name in page and "KiB" in page
    assert "/fleet/restore" not in page and "hx-post=\"/fleet/restore" not in page


def test_read_only_hides_controls_and_refuses_posts(home):
    seed_phase(home, "T-2", Phase.IMPLEMENTING)
    claims.write_claim(home, "T-2", 2**22 + 12345, "r")
    backup.create_backup(load(str(home)), store.now_epoch())
    with TestClient(create_app(home, read_only=True)) as c:
        page = c.get("/fleet").text
        assert 'id="claims"' in page and 'id="backup-files"' in page
        for marker in ("/fleet/up", "/fleet/down", "/fleet/sweep", "/fleet/dry-run",
                       "/fleet/clear-rate-limit", "/fleet/purge-claims"):
            assert marker not in page
        for p in ("/fleet/up", "/fleet/down", "/fleet/sweep", "/fleet/dry-run",
                  "/fleet/clear-rate-limit", "/fleet/purge-claims"):
            assert c.post(p).status_code in (403, 404, 405)
    assert claims.read_claim(home, "T-2") is not None


def test_routes_need_token(home):
    for p in ("/fleet/up", "/fleet/down", "/fleet/sweep", "/fleet/dry-run",
              "/fleet/clear-rate-limit", "/fleet/purge-claims"):
        assert _post(home, p, token=False).status_code == 403


def test_palette_lists_new_actions(home):
    with TestClient(create_app(home)) as c:
        text = c.get("/palette").text
    for label in ("Fleet up", "Fleet down", "Sweep now", "Dry run", "Purge stale claims"):
        assert label in text
    assert "Clear rate limit" not in text
    with TestClient(create_app(home, read_only=True)) as c:
        assert "Sweep now" not in c.get("/palette").text
