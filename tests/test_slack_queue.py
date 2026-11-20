"""T-292: Slack pings are queued by intake and filed by the dispatcher's drain. Only the Slack
read, the code host (``start_review`` seam where noted) and the push command are faked."""
import io
import json
import os
import sys

import pytest

from maestro import claims, cli, dispatcher as disp, event_log, ratelimit, slack_watcher, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.view import fleet as fleet_view

UID = "U111AAA"
PR = "https://github.com/acme/widgets/pull/7"
TOOLS = ["mcp__chat__read_channel"]


def _cfg(home, **kw):
    base = dict(home=home, max_concurrency=3, slack_channel="C0SYNTH", slack_user_id=UID,
                slack_read_tools=list(TOOLS), slack_active_hours="",
                slack_workspace_url="https://chat.example")
    base.update(kw)
    return Config(**base)


def _write_cfg(home, push_log=None):
    extra = f'\n[notify]\nnotify_command = "echo $EVENT $KIND >> {push_log}"\n' if push_log else ""
    (home / "config.toml").write_text(
        '[maestro]\nslack_channel = "C0SYNTH"\n'
        f'slack_user_id = "{UID}"\nslack_read_tools = ["{TOOLS[0]}"]\nslack_active_hours = ""\n'
        'slack_workspace_url = "https://chat.example"\n' + extra)


def _intake_cli(home, messages, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(messages)))
    out = io.StringIO()
    old, sys.stdout = sys.stdout, out
    try:
        code = cli.main(["--home", str(home), "slack-intake"])
    finally:
        sys.stdout = old
    return code, json.loads(out.getvalue())


def _msg(ts="1700000001.000100", **kw):
    return {"ts": ts, "text": f"<@{UID}> {PR}", **kw}


def _pushes(log):
    return log.read_text().split("\n")[:-1] if log.exists() else []


def _boom(cfg, url, source=None, **_kw):
    raise store.MaestroError("code host unreachable")


def test_intake_queues_pushes_once_and_a_sweep_files_the_review(home, monkeypatch, tmp_path):
    log = tmp_path / "pushes.log"
    _write_cfg(home, log)
    code, out = _intake_cli(home, [_msg()], monkeypatch)
    assert code == 0 and len(out["queued"]) == 1 and out["cursor"] == "1700000001.000100"
    state = slack_watcher.load_state(home)
    assert len(state["queue"]) == 1 and state["queue"][0]["attempts"] == 0
    assert len(_pushes(log)) == 1
    assert "requested" in _pushes(log)[0]
    key = "review-acme-widgets-7"
    assert not store.events_path(home, key).exists()  # minted nothing yet
    cfg = _cfg(home)
    disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())
    assert event_log.last_seq(home, key) > 0
    assert slack_watcher.load_state(home)["queue"] == []


def test_the_same_message_twice_is_queued_and_pushed_once(home, monkeypatch, tmp_path):
    log = tmp_path / "pushes.log"
    _write_cfg(home, log)
    _intake_cli(home, [_msg()], monkeypatch)
    _intake_cli(home, [_msg()], monkeypatch)
    # also with the cursor reset, the queued ts is not queued again
    state = slack_watcher.load_state(home)
    state["cursor"] = "0"
    store.write_json(slack_watcher.state_path(home), state)
    _intake_cli(home, [_msg()], monkeypatch)
    assert len(slack_watcher.load_state(home)["queue"]) == 1
    assert len(_pushes(log)) == 1


def test_failing_filing_is_retried_then_dropped_with_a_push(home, tmp_path):
    log = tmp_path / "pushes.log"
    _write_cfg(home, log)
    cfg = _cfg(home, notify_command=f"echo $EVENT >> {log}")
    slack_watcher.intake(cfg, [_msg()], 1.0)
    for _ in range(slack_watcher.MAX_INTAKE_ATTEMPTS):
        assert slack_watcher.load_state(home)["queue"]
        slack_watcher.drain(cfg, start_review=_boom)
    state = slack_watcher.load_state(home)
    assert state["queue"] == [] and len(state["dropped"]) == 1
    assert "unreachable" in state["dropped"][0]["error"]
    assert _pushes(log) == ["requested", "request-failed"]


def test_an_interrupted_drain_has_already_counted_the_attempt(home):
    cfg = _cfg(home)
    slack_watcher.intake(cfg, [_msg()], 1.0)

    def crash(cfg, url, source=None, **_kw):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        slack_watcher.drain(cfg, start_review=crash)
    assert slack_watcher.load_state(home)["queue"][0]["attempts"] == 1


def test_the_queue_is_drained_when_the_poll_is_rate_limited(home):
    cfg = _cfg(home)
    slack_watcher.intake(cfg, [_msg()], 1.0)
    now = store.now_epoch()
    store.write_json(ratelimit._state_path(home), {"paused_until": now + 600, "resets_at": now + 600})
    assert slack_watcher.skip_reason(cfg, now) == "rate limited"
    res = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert event_log.last_seq(home, "review-acme-widgets-7") > 0
    assert slack_watcher.load_state(home)["queue"] == []
    assert res is not None


def test_prompt_has_cursor_and_user_fields_and_items_keep_or_lack_them(home):
    cfg = _cfg(home)
    store.write_json(slack_watcher.state_path(home), {"cursor": "1700000000.000500"})
    prompt = slack_watcher.watcher_command(cfg)
    assert "1700000000.000500" in prompt and '"user"' in prompt and '"user_name"' in prompt
    slack_watcher.intake(cfg, [_msg("1700000001.000100", user="U9", user_name="Ada"),
                               _msg("1700000002.000100")], 1.0)
    q = slack_watcher.load_state(home)["queue"]
    assert (q[0]["user"], q[0]["user_name"]) == ("U9", "Ada")
    assert "user" not in q[1] and "user_name" not in q[1]


def test_a_watcher_older_than_120s_is_reaped_and_the_next_sweep_polls(home):
    cfg = _cfg(home)
    now = store.now_epoch()
    claims.write_claim(home, slack_watcher.WATCHER_KEY, os.getpid(), "w")
    killed = []
    assert disp.run_watchdog(cfg, now + 60, kill=killed.append) == []
    assert disp.run_watchdog(cfg, now + slack_watcher.WATCHER_MAX_SECONDS + 5,
                             kill=killed.append) == [slack_watcher.WATCHER_KEY]
    assert killed
    out = slack_watcher.sync(cfg, DryRunSessions(), now + 3600)
    assert out["spawned"] is True


def test_fleet_panel_shows_queue_length_and_a_dropped_request(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from maestro.web.app import create_app
    _write_cfg(home)
    cfg = _cfg(home)
    slack_watcher.intake(cfg, [_msg("1700000001.000100"), _msg("1700000002.000100", text=None)], 1.0)
    slack_watcher.intake(cfg, [{"ts": "1700000003.000100", "text": f"<@{UID}> {PR}x"}], 2.0)
    for _ in range(slack_watcher.MAX_INTAKE_ATTEMPTS):
        slack_watcher.drain(cfg, start_review=_boom)
    state = slack_watcher.load_state(home)
    assert state["dropped"] and fleet_view.slack_panel(cfg)["dropped"]
    slack_watcher.intake(cfg, [_msg("1700000009.000100")], 3.0)
    with TestClient(create_app(home)) as c:
        html = c.get("/fleet").text
    assert 'id="slack-queue"' in html and "1 queued" in html and "dropped" in html
    assert "code host unreachable" in html
