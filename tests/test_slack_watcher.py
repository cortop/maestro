"""T-255: the Slack PR-ping watcher -- gates, spawn grant, intake verb, fleet panel, doctor.

The Slack plugin is the only mock: `FakeSlack` stands in for what the headless session reads and
pipes to the real `maestro slack-intake` verb. Everything else (CLI, review minting, sweep, fold)
is real. Fixtures are synthetic.
"""
import io
import json
import sys
from datetime import datetime

import pytest

from maestro import cli, dispatcher as disp, event_log, fleet, health, ratelimit, slack_watcher
from maestro import sessions as sessions_mod, snapshot as snap_mod, spend, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.view import fleet as fleet_view

UID = "U111AAA"
PR = "https://github.com/acme/widgets/pull/7"
PR2 = "https://github.com/acme/widgets/pull/8"
TOOLS = ["mcp__chat__read_channel"]


def _cfg(home, **kw):
    base = dict(home=home, max_concurrency=3, slack_channel="C0SYNTH", slack_user_id=UID,
                slack_read_tools=list(TOOLS), slack_active_hours="")
    base.update(kw)
    return Config(**base)


class FakeSlack:
    """The injectable Slack source: channel history, read since a cursor."""

    def __init__(self):
        self.msgs = []

    def post(self, ts, text):
        self.msgs.append({"ts": ts, "text": text,
                          "permalink": f"https://chat.example/archives/C0SYNTH/p{ts.replace('.', '')}"})

    def since(self, cursor):
        k = slack_watcher._ts_key
        return [m for m in self.msgs if k(m["ts"]) > k(cursor)]


def _intake_cli(home, messages, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(messages)))
    out = io.StringIO()
    old, sys.stdout = sys.stdout, out
    try:
        code = cli.main(["--home", str(home), "slack-intake"])
    finally:
        sys.stdout = old
    return code, json.loads(out.getvalue())


def _write_cfg(home):
    (home / "config.toml").write_text(
        '[maestro]\nslack_channel = "C0SYNTH"\n'
        f'slack_user_id = "{UID}"\nslack_read_tools = ["{TOOLS[0]}"]\nslack_active_hours = ""\n')


# --- AC1: interval, active hours, grant -------------------------------------------------------

def test_spawn_grants_only_slack_read_tools_and_intake_verbs(home):
    cfg, sessions = _cfg(home), DryRunSessions()
    assert slack_watcher.sync(cfg, sessions, 1_000_000.0)["spawned"] is True
    (key, _prompt, _cwd, model, _eff, disallowed, allowed, env, _runner, _rm), = sessions.spawned
    assert key == slack_watcher.WATCHER_KEY
    assert allowed == [*TOOLS, "Bash(maestro slack-cursor:*)", "Bash(maestro slack-intake:*)"]
    assert model == "haiku" and env == {"MAESTRO_SLACK_CHANNEL": "C0SYNTH"}
    assert "Bash(gh pr merge:*)" in disallowed


def test_interval_between_polls(home):
    cfg, sessions = _cfg(home, slack_interval=120), DryRunSessions()
    t = 1_000_000.0
    assert slack_watcher.sync(cfg, sessions, t)["spawned"]
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, t + 60) == {"spawned": False, "reason": "not due"}
    assert slack_watcher.sync(cfg, sessions, t + 121)["spawned"]
    assert len(sessions.spawned) == 2


def test_slower_interval_outside_active_hours(home):
    noon = datetime(2026, 3, 4, 12, 0).timestamp()
    night = datetime(2026, 3, 4, 23, 0).timestamp()
    cfg = _cfg(home, slack_active_hours="09:00-19:00", slack_interval=120, slack_idle_interval=1800)
    assert slack_watcher.interval_for(cfg, noon) == 120
    assert slack_watcher.interval_for(cfg, night) == 1800
    sessions = DryRunSessions()
    assert slack_watcher.sync(cfg, sessions, night)["spawned"]
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, night + 600)["reason"] == "not due"
    assert slack_watcher.sync(cfg, sessions, night + 1801)["spawned"]


def test_wraparound_active_hours(home):
    cfg = _cfg(home, slack_active_hours="22:00-06:00")
    assert slack_watcher.in_active_hours(cfg, datetime(2026, 3, 4, 23, 30).timestamp())
    assert slack_watcher.in_active_hours(cfg, datetime(2026, 3, 4, 2, 0).timestamp())
    assert not slack_watcher.in_active_hours(cfg, datetime(2026, 3, 4, 12, 0).timestamp())


def test_dark_until_configured(home):
    sessions = DryRunSessions()
    assert slack_watcher.sync(Config(home=home), sessions, 1.0) == {"spawned": False,
                                                                    "reason": "not configured"}
    assert sessions.spawned == []


def test_real_sweep_spawns_the_watcher_once_per_interval(home):
    cfg, sessions = _cfg(home), DryRunSessions()
    now = 2_000_000.0
    disp.dispatch(cfg, sessions, now)
    assert [s[0] for s in sessions.spawned] == [slack_watcher.WATCHER_KEY]
    sessions._active.clear()
    disp.dispatch(cfg, sessions, now + 30)
    assert len(sessions.spawned) == 1


def test_bad_active_hours_fail_config_load_closed(home):
    (home / "config.toml").write_text('[maestro]\nslack_active_hours = "late"\n')
    with pytest.raises(store.MaestroError, match="slack_active_hours"):
        from maestro import config
        config.load(str(home))


# --- AC2: intake keeps direct mentions with a PR link; cursor dedupes ---------------------------

def test_intake_files_reviews_for_direct_mentions_only(home, monkeypatch):
    _write_cfg(home)
    slack = FakeSlack()
    slack.post("1700000001.000100", f"<@{UID}> please look at {PR}")
    slack.post("1700000002.000100", f"<!here> {PR2}")                   # group mention
    slack.post("1700000003.000100", f"<!channel> <!subteam^S1> {PR2}")  # group mentions
    slack.post("1700000004.000100", f"<@{UID}> thoughts? (no link)")
    slack.post("1700000005.000100", f"<@U999ZZZ> {PR2}")                # someone else
    slack.post("1700000006.000100", f"hey <@{UID}|me> <{PR2}|PR 8> thanks")
    code, out = _intake_cli(home, slack.since("0"), monkeypatch)
    assert code == 0
    assert [f["pr_url"] for f in out["filed"]] == [PR, PR2]
    assert out["cursor"] == "1700000006.000100"
    key = "review-acme-widgets-7"
    assert event_log.last_seq(home, key) > 0
    snap = snap_mod.load(home, key)
    assert snap.kind == "review"


def test_review_verb_gets_the_message_permalink_as_source(home):
    cfg, seen = _cfg(home), []

    def fake_review(cfg, url, source=None):
        seen.append((url, source))
        return {"key": "k", "minted": True, "started": True}

    slack = FakeSlack()
    slack.post("1700000001.000100", f"<@{UID}> {PR}")
    slack_watcher.intake(cfg, slack.msgs, 1.0, start_review=fake_review)
    assert seen == [(PR, slack.msgs[0]["permalink"])]


def test_cursor_prevents_reprocessing(home, monkeypatch):
    _write_cfg(home)
    slack = FakeSlack()
    slack.post("1700000001.000100", f"<@{UID}> {PR}")
    _, first = _intake_cli(home, slack.since("0"), monkeypatch)
    assert len(first["filed"]) == 1
    # The same history re-delivered: nothing is filed twice.
    _, again = _intake_cli(home, slack.msgs, monkeypatch)
    assert again["filed"] == []
    # Only the new message is filed.
    slack.post("1700000009.000100", f"<@{UID}> {PR2}")
    cursor = slack_watcher.load_state(home)["cursor"]
    _, third = _intake_cli(home, slack.since(cursor), monkeypatch)
    assert [f["pr_url"] for f in third["filed"]] == [PR2]


def test_first_spawn_starts_the_cursor_at_now_never_history(home):
    cfg = _cfg(home)
    slack_watcher.sync(cfg, DryRunSessions(), 1_800_000_000.0)
    assert slack_watcher.load_state(home)["cursor"] == "1800000000.000000"


def test_failed_filing_is_retried_then_dropped(home):
    cfg = _cfg(home)
    calls = []

    def boom(cfg, url, source=None):
        calls.append(url)
        raise store.MaestroError("code host unreachable")

    msg = {"ts": "1700000001.000100", "text": f"<@{UID}> {PR}", "permalink": "https://chat.example/p1"}
    out = slack_watcher.intake(cfg, [msg], 1.0, start_review=boom)
    assert out["errors"] and slack_watcher.load_state(home)["retry"]
    slack_watcher.intake(cfg, [], 2.0, start_review=boom)
    slack_watcher.intake(cfg, [], 3.0, start_review=boom)
    assert slack_watcher.load_state(home)["retry"] == []
    assert len(calls) == slack_watcher.MAX_INTAKE_ATTEMPTS


def test_slack_cursor_verb_prints_the_cursor(home, capsys):
    _write_cfg(home)
    store.write_json(slack_watcher.state_path(home), {"cursor": "1700000001.000100"})
    assert cli.main(["--home", str(home), "slack-cursor"]) == 0
    assert capsys.readouterr().out.strip() == "1700000001.000100"


# --- AC3: budget exclusion, own cap, spend meter, brakes ----------------------------------------

def test_not_in_spawn_ledger_budget_or_floor(home):
    cfg, sessions = _cfg(home), DryRunSessions()
    now = 2_000_000.0
    disp.dispatch(cfg, sessions, now)
    assert [s[0] for s in sessions.spawned] == [slack_watcher.WATCHER_KEY]
    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    assert slack_watcher.WATCHER_KEY not in ledger
    assert health.spawn_rate(home, now)["total"] == 0


def test_own_daily_cap(home):
    cfg, sessions = _cfg(home, slack_daily_cap=2, slack_interval=10), DryRunSessions()
    t = 3_000_000.0
    results = []
    for i in range(4):
        sessions._active.clear()
        results.append(slack_watcher.sync(cfg, sessions, t + i * 20))
    assert [r["spawned"] for r in results] == [True, True, False, False]
    assert results[2]["reason"] == "daily cap reached"
    # A new UTC day resets it.
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, t + 86_400)["spawned"]


def test_brakes_paused_rate_limited_over_ceiling(home):
    cfg, sessions = _cfg(home, daily_spend_ceiling_usd=1.0), DryRunSessions()
    t = 4_000_000.0
    fleet.pause(home, reason="test")
    assert slack_watcher.sync(cfg, sessions, t)["reason"] == "fleet paused"
    fleet.resume(home)
    store.write_json(ratelimit._state_path(home), {"paused_until": t + 600, "resets_at": t + 600})
    assert slack_watcher.sync(cfg, sessions, t)["reason"] == "rate limited"
    ratelimit._state_path(home).unlink()
    store.write_json(spend._state_path(home), {"date": store.utc_date(t), "total_usd": 5.0})
    assert slack_watcher.sync(cfg, sessions, t)["reason"] == "over spend ceiling"
    assert sessions.spawned == []
    store.write_json(spend._state_path(home), {"date": store.utc_date(t), "total_usd": 0.1})
    assert slack_watcher.sync(cfg, sessions, t)["spawned"]


def test_watcher_cost_counts_in_the_spend_meter(home):
    cfg = _cfg(home)
    now = store.now_epoch()
    log = store.session_stream_path(home, slack_watcher.WATCHER_KEY, f"{sessions_mod.session_name(slack_watcher.WATCHER_KEY)}-{now:.6f}")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "result", "total_cost_usd": 0.0123, "num_turns": 3}) + "\n")
    state = spend.probe(cfg, now)  # the watcher is NOT in the spawn ledger
    assert state["total_usd"] == pytest.approx(0.0123)
    assert slack_watcher.spend_today(cfg, now) == pytest.approx(0.0123)


def test_watchdog_reaps_a_stuck_watcher_without_touching_ticket_state(home):
    cfg = _cfg(home, max_session_seconds=60)
    from maestro import claims
    import os
    claims.write_claim(home, slack_watcher.WATCHER_KEY, os.getpid(), "w")
    killed = []
    reaped = disp.run_watchdog(cfg, store.now_epoch() + 3600, kill=killed.append)
    assert reaped == [slack_watcher.WATCHER_KEY] and killed
    assert not store.events_path(home, slack_watcher.WATCHER_KEY).exists()


# --- AC4: fleet panel and doctor ---------------------------------------------------------------

def test_fleet_panel_reports_poll_cap_and_spend(home):
    cfg = _cfg(home, slack_daily_cap=50)
    now = store.now_epoch()
    assert fleet_view.slack_panel(cfg, now)["last_poll"] is None
    slack_watcher.sync(cfg, DryRunSessions(), now)
    slack_watcher.intake(cfg, [], now + 1)
    panel = fleet_view.slack_panel(cfg, now + 2)
    assert panel["enabled"] and panel["polls_today"] == 1 and panel["cap"] == 50
    assert panel["last_poll"] == now + 1


def test_web_fleet_page_shows_the_watcher_panel(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from maestro.web.app import create_app
    _write_cfg(home)
    cfg = _cfg(home)
    slack_watcher.sync(cfg, DryRunSessions(), store.now_epoch())
    with TestClient(create_app(home)) as c:
        html = c.get("/fleet").text
    assert 'id="slack-watcher"' in html and "1/300 polls today" in html


def test_doctor_warns_when_slack_tools_unavailable(home):
    cfg = _cfg(home)
    ok = health.check_slack_tools(
        cfg, 0, probe=lambda: {"chat": True})
    assert ok["status"] == "ok"
    down = health.check_slack_tools(cfg, 0, probe=lambda: {"other": True})
    assert down["status"] == "warn" and down["missing"] == TOOLS
    unauth = health.check_slack_tools(cfg, 0, probe=lambda: {"chat": False})
    assert unauth["status"] == "warn"
    assert health.check_slack_tools(cfg, 0, probe=lambda: None)["status"] == "warn"
    assert health.check_slack_tools(Config(home=home), 0)["status"] == "ok"
    assert health.CHECKS.index(health.check_slack_tools) > health.CHECKS.index(
        health.check_serving_checkout)


def test_doctor_parses_claude_mcp_list(home):
    class P:
        returncode = 0
        stdout = ("plugin:chat:chat: https://x - ✓ Connected\n"
                  "mcp-bad: https://y - ✗ Failed to connect\n")

    states = health._mcp_server_states(run=lambda *a, **k: P())
    assert states == {"plugin_chat_chat": True, "mcp_bad": False}
