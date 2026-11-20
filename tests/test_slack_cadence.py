"""T-288: the Slack watcher polls on every sweep -- due test, capacity, hook order, liveness, intake, log pruning.

Only the `claude -p` spawn is mocked (`DryRunSessions`); sweeps, CLI and pruning are real.
"""
import io
import json
import os
import sys

from maestro import snapshot as snap_mod
from maestro import cli, claims, dispatcher as disp, ops, slack_watcher
from maestro.config import Config
from maestro.sessions import DryRunSessions
from tests.conftest import seed_phase
from maestro.statemachine import Phase

T0 = 1_700_000_000.0


def _cfg(home, **kw):
    base = dict(home=home, max_concurrency=2, slack_channel="C0SYNTH", slack_user_id="U1",
                slack_read_tools=["mcp__chat__read_channel"], slack_active_hours="",
                slack_interval=60)
    base.update(kw)
    return Config(**base)


def _spawns(sessions):
    return [s for s in sessions.spawned if s[0] == slack_watcher.WATCHER_KEY]


def test_every_sweep_spawns_at_60s(tmp_path):
    cfg, sessions = _cfg(tmp_path), DryRunSessions()
    got = []
    for dt in (0, 58, 119, 181):
        sessions._active.clear()
        got.append(slack_watcher.sync(cfg, sessions, T0 + dt)["spawned"])
    assert got == [True, True, True, True]
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, T0 + 191)["reason"] == "not due"


def test_zero_interval_floor(tmp_path):
    cfg, sessions = _cfg(tmp_path, slack_interval=0), DryRunSessions()
    assert slack_watcher.sync(cfg, sessions, T0)["spawned"]
    sessions._active.clear()
    assert not slack_watcher.sync(cfg, sessions, T0 + 20)["spawned"]


def test_default_config():
    cfg = Config(home=None)
    assert cfg.slack_interval == 60
    start, end = slack_watcher.parse_active_hours(cfg.slack_active_hours)
    active_polls = (end - start) * 60 // 60
    idle_polls = (24 * 3600 - (end - start) * 60) // cfg.slack_idle_interval + 1
    assert cfg.slack_daily_cap >= active_polls + idle_polls


def test_watcher_ignores_capacity_and_takes_no_slot(tmp_path):
    cfg = _cfg(tmp_path)
    for k in ("T-1", "T-2"):
        seed_phase(tmp_path, k, Phase.READY)
    sessions = DryRunSessions(active={"T-8", "T-9"})  # max_concurrency reconcilers live
    assert slack_watcher.sync(cfg, sessions, T0)["spawned"]
    # a live watcher does not reduce the reconcilers a real sweep spawns
    sessions = DryRunSessions(active={slack_watcher.WATCHER_KEY})
    disp.dispatch(cfg, sessions, T0 + 1)
    assert len([s for s in sessions.spawned if s[0] != slack_watcher.WATCHER_KEY]) == 2


def test_hook_runs_before_sync_worktrees(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(disp, "sync_worktrees", lambda *a, **k: order.append("worktrees") or {})
    monkeypatch.setattr(slack_watcher, "sync", lambda *a, **k: order.append("slack") or {})
    disp.dispatch(_cfg(tmp_path), DryRunSessions(), T0)
    assert order.index("slack") < order.index("worktrees")


def test_dead_zero_turn_claim_still_handled(tmp_path):
    cfg = _cfg(tmp_path)
    seed_phase(tmp_path, "T-5", Phase.IMPLEMENTING)
    log = tmp_path / "agent-logs" / "T-5" / "s.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"type": "result", "num_turns": 0, "result": "Unknown command"}) + "\n")
    claims.write_claim(tmp_path, "T-5", 2_999_999, "n", log_path=str(log), cwd="/x", prompt="/bad T-5")
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, T0)  # the watcher is due in this same sweep
    assert _spawns(sessions)
    assert "T-5" in report.reaped or snap_mod.load(tmp_path, "T-5").failure_count >= 1


def test_sync_does_not_release_dead_claims(tmp_path):
    cfg = _cfg(tmp_path)
    claims.write_claim(tmp_path, "T-5", 2_999_999, "n")
    slack_watcher.sync(cfg, DryRunSessions(), T0)
    assert claims.read_claim(tmp_path, "T-5") is not None


def test_empty_intake_records_poll(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("")
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    assert cli.main(["--home", str(tmp_path), "slack-intake"]) == 0
    assert isinstance(slack_watcher.load_state(tmp_path).get("last_poll_ts"), float)
    assert "even when it is empty" in slack_watcher.watcher_command(_cfg(tmp_path))


def test_watcher_logs_pruned_after_two_days(tmp_path):
    cfg = _cfg(tmp_path, session_log_retention_days=14)
    now = T0
    for key in (slack_watcher.WATCHER_KEY, "T-5"):
        d = tmp_path / "agent-logs" / key
        d.mkdir(parents=True)
        for age in (3, 1):
            f = d / f"reconcile-{key}-{now - age * 86400:.6f}.log"
            f.write_text("x")
            os.utime(f, (now - age * 86400, now - age * 86400))
    ops.prune_all_session_logs(cfg, now=now)
    left = lambda k: len(list((tmp_path / "agent-logs" / k).glob("*.log")))
    assert left(slack_watcher.WATCHER_KEY) == 1
    assert left("T-5") == 2


def test_guards_still_hold(tmp_path):
    cfg = _cfg(tmp_path, slack_daily_cap=2)
    sessions = DryRunSessions()
    assert slack_watcher.sync(cfg, sessions, T0)["spawned"]
    assert slack_watcher.sync(cfg, sessions, T0 + 100)["reason"] == "already running"
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, T0 + 100)["spawned"]
    sessions._active.clear()
    assert slack_watcher.sync(cfg, sessions, T0 + 200)["reason"] == "daily cap reached"
