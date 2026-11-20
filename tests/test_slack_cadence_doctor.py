"""T-289: `maestro doctor` and the fleet page state the Slack watcher's real poll cadence."""
import pytest

from maestro import fleet, health, slack_watcher, store
from maestro.config import Config
from tests.conftest import run_doctor

T0 = 1_700_000_000.0


def _cfg(home, **kw):
    base = dict(home=home, slack_channel="C0SYNTH", slack_user_id="U1",
                slack_read_tools=["mcp__chat__read_channel"], slack_active_hours="",
                slack_interval=60, slack_daily_cap=0)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def sweep(monkeypatch):
    box = {"v": 60}
    monkeypatch.setattr(fleet, "_interval_from_plist", lambda *a, **k: box["v"])
    return box


def _check(cfg, now=T0):
    return health.check_slack_cadence(cfg, now)


def test_ok_when_unconfigured(tmp_path, sweep):
    r = health.check_slack_cadence(Config(home=tmp_path), T0)
    assert r["status"] == "ok"


def test_ok_when_cadence_matches(tmp_path, sweep):
    assert _check(_cfg(tmp_path))["status"] == "ok"


def test_warns_when_sweep_exceeds_interval(tmp_path, sweep):
    sweep["v"] = 300
    r = _check(_cfg(tmp_path))
    assert r["status"] == "warn" and "300s" in r["detail"] and "fleet up --interval 60" in r["detail"]


def test_warns_when_cap_below_needed(tmp_path, sweep):
    r = _check(_cfg(tmp_path, slack_daily_cap=100))
    assert r["status"] == "warn" and "below" in r["detail"]


def test_warns_when_cap_reached(tmp_path, sweep):
    cfg = _cfg(tmp_path, slack_daily_cap=2000)
    store.write_json(slack_watcher.state_path(tmp_path), {"polls": {store.utc_date(T0): 2000}})
    r = _check(cfg)
    assert r["status"] == "warn" and "reached" in r["detail"]


def test_warns_when_last_poll_stale(tmp_path, sweep):
    cfg = _cfg(tmp_path)
    store.write_json(slack_watcher.state_path(tmp_path), {"last_poll_ts": T0 - 400})
    r = _check(cfg)
    assert r["status"] == "warn" and "skip reason" in r["detail"]
    store.write_json(slack_watcher.state_path(tmp_path), {"last_poll_ts": T0 - 100})
    assert _check(cfg)["status"] == "ok"


def test_doctor_verb_reports_check(home, sweep):
    _, report = run_doctor(home)
    assert any(c["name"] == "slack_cadence" and c["status"] == "ok" for c in report["checks"])


def test_polls_needed_per_day(tmp_path):
    cfg = _cfg(tmp_path, slack_active_hours="09:00-19:00", slack_idle_interval=1800)
    assert slack_watcher.polls_needed_per_day(cfg) == 628


def test_panel_has_effective_interval_and_next_poll(tmp_path, sweep):
    sweep["v"] = 300
    cfg = _cfg(tmp_path)
    store.write_json(slack_watcher.state_path(tmp_path), {"last_spawn_ts": T0 - 100})
    p = slack_watcher.panel(cfg, T0)
    assert p["effective_interval"] == 300 and p["next_poll_in"] == 200
    assert p["polls_needed"] == slack_watcher.polls_needed_per_day(cfg)


def test_fleet_page_renders_cadence(home, sweep):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from maestro.web.app import create_app
    (home / "config.toml").write_text(
        '[maestro]\nslack_channel = "C0SYNTH"\nslack_user_id = "U1"\n'
        'slack_read_tools = ["mcp__chat__read_channel"]\nslack_active_hours = ""\n')
    with TestClient(create_app(home)) as c:
        html = c.get("/fleet").text
    assert 'id="slack-cadence"' in html and "polls every 60s" in html and "next poll in" in html
