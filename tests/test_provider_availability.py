"""MTO-8: board-level visibility when the model provider is down or the box is
offline -- `health.check_provider_availability` (primary signal: consecutive
non-429 error outcomes across the fleet's own session logs; confirmation
probe: a bare TCP connect, only ever reached once that primary signal has
already tripped) and its `alarm.py` condition.
"""
import io
import json
import sys

from maestro import alarm, claims, cli, health, store
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


def _write_opencode_session(home, key, epoch, reason):
    """An opencode-runner session log (OC-5's own verified vocabulary) -- a
    different provider than the Anthropic one this check probes."""
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = store.session_opencode_path(home, key, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "step_start", "part": {}},
             {"type": "step_finish", "part": {"reason": reason, "cost": 0}}]
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return path


def _write_pi_session(home, key, epoch, outcome, *, provider="anthropic"):
    """A pi-runner session log (T-58's own verified vocabulary) -- unlike
    opencode, THIS is meant to count toward the streak (AC6), and its own
    ``provider`` field is what resolves the confirmation probe's host (AC7)."""
    session_id = f"reconcile-{key}-{epoch:.6f}"
    path = store.session_pi_path(home, key, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session", "version": 3, "id": session_id,
              "timestamp": "2026-08-14T00:00:00.000Z", "cwd": str(home)},
             {"type": "agent_start"}, {"type": "turn_start"}]
    if outcome == "success":
        lines.append({"type": "message_end",
                       "message": {"role": "assistant", "stopReason": "stop",
                                   "provider": provider}})
    elif outcome == "error":
        lines.append({"type": "message_end",
                       "message": {"role": "assistant", "stopReason": "error",
                                   "errorMessage": "401 auth error", "provider": provider}})
    if outcome in ("success", "error"):
        lines.append({"type": "agent_end", "messages": [], "willRetry": False})
    # "running": no agent_end at all.
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return path


def _boom_probe(host=None):
    raise AssertionError("must not call the probe when the primary signal hasn't tripped")


# --- AC3: no terminal session outcomes at all -- measure, don't fail open -----


def test_ok_on_a_fresh_home_with_no_sessions_and_a_reachable_probe(cfg, home):
    """A fresh home has no session history to derive a streak from, but T-89
    (AC3) no longer skips the probe here -- reachability is now MEASURED, not
    asserted. A reachable probe still reports ok."""
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "reachable"))
    assert result["status"] == "ok"
    assert result["state"] == "ok"
    assert result["error_streak"] == 0


def test_no_network_on_a_fresh_home_with_no_sessions_and_an_unreachable_probe(cfg, home):
    """AC3: the fresh-home fail-open this test used to pin (a bare `ok` with
    a "provider reachable" detail it never measured) is gone -- an
    unreachable probe now reports a distinguishable non-ok state and the
    detail no longer claims reachability."""
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (False, "unreachable"))
    assert result["status"] != "ok"
    assert result["state"] != "ok"
    assert "provider reachable" not in result["detail"]
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
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "reachable"))
    assert result["status"] == "warn"
    assert result["state"] == "erroring"
    assert result["error_streak"] == 3
    assert "3 consecutive" in result["detail"]


def test_no_network_when_streak_trips_and_probe_cannot_reach_the_network(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (False, "unreachable"))
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
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "ok"))
    assert result["error_streak"] == 3
    assert result["state"] == "erroring"


def test_opencode_runner_errors_never_count_toward_the_anthropic_streak(cfg, home):
    """OC-5: now that `steplog.session_outcome` can classify an opencode log's
    own success/error (it used to always report 'unknown' here), this check
    must still never fold it in -- it probes api.anthropic.com, not ollama.
    Every session here is opencode, so `_recent_fleet_outcomes` sees NO
    stream-json/pi outcomes at all -- same "no terminal session outcomes"
    shape AC3 covers, hence the probe (unlike the other below-threshold
    tests above) IS reached here; a reachable one still reports ok."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_opencode_session(home, "T-1", 100.0, "error")
    _write_opencode_session(home, "T-1", 200.0, "error")
    _write_opencode_session(home, "T-1", 300.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "reachable"))
    assert result["status"] == "ok"
    assert result["error_streak"] == 0


def test_pi_runner_errors_count_toward_the_streak_and_trip_it(cfg, home):
    """AC6 (T-58): a board whose three most recent sessions are pi error logs
    trips the streak through the real check -- unlike opencode, pi IS counted."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_pi_session(home, "T-1", epoch, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "reachable"))
    assert result["status"] == "warn"
    assert result["state"] == "erroring"
    assert result["error_streak"] == 3


def test_pi_confirmation_probe_host_resolves_from_the_erroring_sessions_own_provider(cfg, home):
    """AC7: the confirmation probe is asked about the HOST the erroring pi
    sessions actually recorded talking to (their own ``provider`` field), not
    the hardcoded Anthropic default -- captured via an injected probe."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_pi_session(home, "T-1", epoch, "error", provider="google")
    captured = []

    def _capture_probe(host):
        captured.append(host)
        return True, "reachable"

    result = health.check_provider_availability(cfg, 1000, probe=_capture_probe)
    assert result["status"] == "warn"
    assert captured == ["generativelanguage.googleapis.com"]


def test_claude_streak_still_probes_the_hardcoded_anthropic_host(cfg, home):
    """AC7 counterpart: a Claude (`stream-json`) streak's confirmation probe
    is unchanged -- still `_PROVIDER_PROBE_HOST` -- since Claude has no
    per-session provider field the way pi does."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    captured = []

    def _capture_probe(host):
        captured.append(host)
        return True, "reachable"

    health.check_provider_availability(cfg, 1000, probe=_capture_probe)
    assert captured == [health._PROVIDER_PROBE_HOST]


def test_probe_bounded_to_once_per_configured_interval_across_report_calls(cfg, home):
    """AC3's cost bound: a fresh home's confirmation probe is real network I/O
    -- report() run repeatedly (a doctor sweep, a TUI badge refresh) must not
    turn that into a probe storm. Two `report()` calls a few seconds apart,
    well inside `provider_probe_interval_s`, hit the probe once; a third call
    past the interval probes again."""
    calls = []

    def _probe(host):
        calls.append(host)
        return True, "reachable"

    cfg.provider_probe_interval_s = 300
    r1 = health.check_provider_availability(cfg, 1000, probe=_probe)
    r2 = health.check_provider_availability(cfg, 1050, probe=_probe)
    assert len(calls) == 1
    assert r1["state"] == r2["state"] == "ok"

    r3 = health.check_provider_availability(cfg, 1000 + 301, probe=_probe)
    assert len(calls) == 2
    assert r3["state"] == "ok"


def test_probe_interval_zero_disables_caching(cfg, home):
    calls = []

    def _probe(host):
        calls.append(host)
        return True, "reachable"

    cfg.provider_probe_interval_s = 0
    health.check_provider_availability(cfg, 1000, probe=_probe)
    health.check_provider_availability(cfg, 1000, probe=_probe)
    assert len(calls) == 2


def test_crashed_session_with_a_live_claim_and_dead_pid_counts_toward_the_streak(cfg, home):
    """T-89 (Gap 3, AC4): a session log holding only non-JSON runner output
    (a stderr splat) whose claim's pid is dead is classified `crashed` by
    `steplog.session_outcome` (threaded through via the claim's own
    `log_path`, see `_recent_fleet_outcomes`) and counts toward the streak
    the same as a parsed `error` result."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    session_id = f"reconcile-T-1-{100.0:.6f}"
    crashed_log = store.session_stream_path(home, "T-1", session_id)
    crashed_log.parent.mkdir(parents=True, exist_ok=True)
    crashed_log.write_text("zsh: command not found: claude\n", encoding="utf-8")
    claims.write_claim(home, "T-1", -1, "reconcile-T-1", log_path=str(crashed_log))
    _write_session(home, "T-1", 200.0, "error")
    _write_session(home, "T-1", 300.0, "error")

    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "reachable"))
    assert result["error_streak"] == 3
    assert result["state"] == "erroring"


def test_a_running_session_in_the_window_is_skipped_not_counted(cfg, home):
    seed_ticket(home, "T-1", "x", phase="implementing")
    _write_session(home, "T-1", 100.0, "error")
    _write_session(home, "T-1", 200.0, "error")
    _write_session(home, "T-1", 300.0, "running")  # newest, no terminal result yet
    _write_session(home, "T-1", 400.0, "error")
    result = health.check_provider_availability(cfg, 1000, probe=lambda host: (True, "ok"))
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
    """AC3: a fresh home now genuinely probes (no session history to fail
    open on) -- a reachable probe still reports ok."""
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))
    code, out = _sweep(home)
    assert code == 0
    check = next(c for c in out["checks"] if c["name"] == "provider_availability")
    assert check["status"] == "ok"


def test_real_doctor_json_reports_no_network(home, monkeypatch):
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "no route to host"))
    code, out = _sweep(home)
    assert code == 0  # WARN/FAIL-only, never blocks a spawn (report-only, MTO-8)
    check = next(c for c in out["checks"] if c["name"] == "provider_availability")
    assert check["status"] == "fail"
    assert check["state"] == "no_network"


def test_real_doctor_strict_exits_nonzero_on_no_network(home, monkeypatch):
    """AC6: `maestro doctor --strict` exits non-zero when the provider check's
    state is no_network -- report-only never blocks a plain sweep, but
    --strict is the opt-in gate a human/CI runs to demand a fully green
    board."""
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "no route to host"))
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor", "--strict"])
    finally:
        sys.stdout = old
    assert code != 0
    out = json.loads(buf.getvalue())
    check = next(c for c in out["checks"] if c["name"] == "provider_availability")
    assert check["state"] == "no_network"


# --- alarm.py condition: fires through the existing transport, deduped --------


def test_alarm_fires_once_on_a_fresh_no_network_episode(home, tmp_path, monkeypatch):
    log_path = tmp_path / "alarm.log"
    seed_ticket(home, "T-1", "x", phase="implementing")
    for epoch in (100.0, 200.0, 300.0):
        _write_session(home, "T-1", epoch, "error")
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "offline"))
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
    """AC3 changed a fresh board from 'never probes' to 'probes, rate-bounded'
    -- a genuinely healthy (reachable) board must still never alarm, which is
    this test's real intent; only the probe's own reachability, not whether
    it fires at all, is what changed."""
    log_path = tmp_path / "alarm.log"
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))
    cfg = Config(home=home,
                 notify_command=f'printf "%s|%s\\n" "$KEY" "$PHASE" >> {log_path}')

    fired = alarm.check(cfg, 1_000_000, health, runaway_reason=None, spend_ceiling_reason=None)
    assert fired == []
    assert not log_path.exists()
