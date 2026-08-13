"""MTO-8: board-level visibility when the model provider is down or the box is
offline -- `health.check_provider_availability` (primary signal: consecutive
non-429 error outcomes across the fleet's own session logs; confirmation
probe: a bare TCP connect, only ever reached once that primary signal has
already tripped) and its `alarm.py` condition.
"""
import io
import json
import sys

from maestro import alarm, cli, health, store
from maestro.config import Config

from conftest import seed_ticket


def _write_session(home, key, epoch, outcome):
    """A session log filename `sessions.list_sessions` recognizes
    (`reconcile-<key>-<epoch>.stream.jsonl`), with just enough of a stream-json
    body for `steplog.session_outcome` to classify it as *outcome*."""
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = store.session_stream_path(home, key, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "system", "subtype": "init", "session_id": session_id}]
    if outcome == "success":
        lines.append({"type": "result", "subtype": "success", "is_error": False})
    elif outcome == "error":
        lines.append({"type": "result", "subtype": "error_during_execution", "is_error": True})
    elif outcome == "rate_limited":
        lines.append({"type": "result", "subtype": "success", "is_error": True,
                       "api_error_status": 429})
    # "running": no terminal result line at all.
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return path


def _boom_probe():
    raise AssertionError("must not call the probe when the primary signal hasn't tripped")


# --- fail open when nothing configured / no evidence of a problem --------------


def test_ok_and_no_probe_call_on_a_fresh_home_with_no_sessions(cfg, home):
    result = health.check_provider_availability(cfg, 1000, probe=_boom_probe)
    assert result["status"] == "ok"
    assert result["state"] == "ok"
    assert result["error_streak"] == 0


def test_ok_when_error_streak_below_threshold(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_session(home, "T-1", 100.0, "error")
    _write_session(home, "T-1", 200.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=_boom_probe)
    assert result["status"] == "ok"
    assert result["error_streak"] == 2


def test_success_among_the_scanned_window_keeps_it_ok(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_session(home, "T-1", 100.0, "success")
    _write_session(home, "T-1", 200.0, "error")
    _write_session(home, "T-1", 300.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=_boom_probe)
    assert result["status"] == "ok"


# --- primary signal trips -> confirmation probe distinguishes the two states ---


def test_erroring_when_streak_trips_and_probe_reaches_the_network(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for i, epoch in enumerate((100.0, 200.0, 300.0)):
        _write_session(home, "T-1", epoch, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda: (True, "reachable"))
    assert result["status"] == "warn"
    assert result["state"] == "erroring"
    assert result["error_streak"] == 3
    assert "3 consecutive" in result["detail"]


def test_no_network_when_streak_trips_and_probe_cannot_reach_the_network(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda: (False, "unreachable"))
    assert result["status"] == "fail"
    assert result["state"] == "no_network"


# --- rate limiting is a distinct, never-conflated signal -----------------------


def test_rate_limited_breaks_the_streak_and_never_calls_the_probe(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_session(home, "T-1", 100.0, "error")
    _write_session(home, "T-1", 200.0, "error")
    _write_session(home, "T-1", 300.0, "rate_limited")  # newest -- breaks the streak
    result = health.check_provider_availability(cfg, 1000, probe=_boom_probe)
    assert result["status"] == "ok"
    assert result["error_streak"] == 0


# --- fleet-wide, across tickets ------------------------------------------------


def test_streak_is_computed_fleet_wide_across_tickets(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    seed_ticket(home, "T-2", "y", phase="qa")
    _write_session(home, "T-1", 100.0, "error")
    _write_session(home, "T-2", 200.0, "error")
    _write_session(home, "T-1", 300.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda: (True, "ok"))
    assert result["error_streak"] == 3
    assert result["state"] == "erroring"


def test_a_running_session_in_the_window_is_skipped_not_counted(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_session(home, "T-1", 100.0, "error")
    _write_session(home, "T-1", 200.0, "error")
    _write_session(home, "T-1", 300.0, "running")  # newest, no terminal result yet
    _write_session(home, "T-1", 400.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda: (True, "ok"))
    # the "running" entry contributes no verdict -- the three error outcomes
    # (400, 200, 100) are still a fresh streak of 3
    assert result["error_streak"] == 3
    assert result["state"] == "erroring"


# --- registered in CHECKS / real `maestro doctor --json` -----------------------


def test_check_name_appears_in_the_doctor_registry(cfg):
    names = {r["name"] for r in health.run_checks(cfg, 1000)}
    assert "provider_availability" in names


def _sweep(home):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


def test_real_doctor_json_ok_on_a_fresh_home(home, monkeypatch):
    monkeypatch.setattr(health, "_default_provider_probe", _boom_probe)
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "provider_availability")
    assert check["status"] == "ok"


def test_real_doctor_json_reports_no_network(home, monkeypatch):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    monkeypatch.setattr(health, "_default_provider_probe", lambda: (False, "no route to host"))
    code, out = _sweep(home)
    assert code == 0  # WARN/FAIL-only, never blocks a spawn (report-only, MTO-8)
    check = next(c for c in out["checks"] if c["name"] == "provider_availability")
    assert check["status"] == "fail"
    assert check["state"] == "no_network"


# --- alarm.py condition: fires through the existing transport, deduped --------


def test_alarm_fires_once_on_a_fresh_no_network_episode(home, tmp_path, monkeypatch):
    log_path = tmp_path / "alarm.log"
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    monkeypatch.setattr(health, "_default_provider_probe", lambda: (False, "offline"))
    cfg = Config(home=home, alarm_cooldown_s=50,
                 notify_command=f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}')

    fired = alarm.check(cfg, 1_000_000, health, runaway_reason=None, spend_ceiling_reason=None)
    assert fired == ["provider_availability"]
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].split("|", 1)[1] == "alarm:provider-availability"

    # still active, well inside cooldown -- no re-fire
    fired2 = alarm.check(cfg, 1_000_010, health, runaway_reason=None, spend_ceiling_reason=None)
    assert fired2 == []
    assert log_path.read_text().splitlines() == lines


def test_alarm_never_fires_on_a_healthy_board_and_never_probes(home, tmp_path, monkeypatch):
    log_path = tmp_path / "alarm.log"
    monkeypatch.setattr(health, "_default_provider_probe", _boom_probe)
    cfg = Config(home=home,
                 notify_command=f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}')

    fired = alarm.check(cfg, 1_000_000, health, runaway_reason=None, spend_ceiling_reason=None)
    assert fired == []
    assert not log_path.exists()
