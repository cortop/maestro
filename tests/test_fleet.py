"""`maestro fleet` — tested against a fake launchctl / install script (no real system)."""
import re

from maestro import fleet, store


class FakeProc:
    def __init__(self, rc=0, stdout=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = ""


def test_up_calls_script_with_interval(home):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return FakeProc(0, "loaded")

    res = fleet.up(home, interval=120, run=run, script="/fake/install.sh")
    assert res["action"] == "up" and res["interval"] == 120 and res["rc"] == 0
    assert calls[0] == ["/fake/install.sh", "up", "--interval", "120"]


def test_down_calls_script(home):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return FakeProc(0)

    res = fleet.down(home, run=run, script="/fake/install.sh")
    assert res["rc"] == 0 and calls[0] == ["/fake/install.sh", "down"]


def test_status_loaded_with_heartbeat(home):
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"epoch": store.now_epoch() - 10})

    def run(cmd, **kw):
        return FakeProc(0, "123\t0\tcom.maestro.dispatcher\nother\t0\tcom.apple.foo\n")

    res = fleet.status(home, run=run, plist="/nonexistent")
    assert res["loaded"] is True
    assert 0 <= res["heartbeat_age_s"] <= 120
    assert res["interval"] is None  # no plist on disk


def test_status_not_loaded_without_heartbeat(home):
    def run(cmd, **kw):
        return FakeProc(0, "456\t0\tcom.apple.something\n")

    res = fleet.status(home, run=run, plist="/nonexistent")
    assert res["loaded"] is False
    assert res["heartbeat_age_s"] is None


def test_interval_parsed_from_plist(home, tmp_path):
    plist = tmp_path / "agent.plist"
    plist.write_text("<key>StartInterval</key>\n  <integer>600</integer>\n")
    assert fleet._interval_from_plist(plist) == 600


# --- runaway regression (2026-07-19): the cadence must have a floor --------------

def test_clamp_interval_floors_small_and_junk_values():
    assert fleet.clamp_interval(300) == 300
    assert fleet.clamp_interval(fleet.MIN_INTERVAL) == fleet.MIN_INTERVAL
    assert fleet.clamp_interval(10) == fleet.MIN_INTERVAL   # the incident's value
    assert fleet.clamp_interval(0) == fleet.MIN_INTERVAL
    assert fleet.clamp_interval(-5) == fleet.MIN_INTERVAL
    assert fleet.clamp_interval("nonsense") == fleet.MIN_INTERVAL
    assert fleet.clamp_interval(None) == fleet.MIN_INTERVAL


def test_up_clamps_a_dangerous_interval_before_calling_the_script(home):
    """`fleet.up` is the only Python path to install.sh (CLI and TUI both route
    through it), so the floor has to bind here. `--interval 10` is what actually
    ran on 2026-07-19."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return FakeProc(0, "loaded")

    res = fleet.up(home, interval=10, run=run, script="/fake/install.sh")
    assert calls[0] == ["/fake/install.sh", "up", "--interval", str(fleet.MIN_INTERVAL)]
    assert res["interval"] == fleet.MIN_INTERVAL
    assert res["clamped_from"] == 10


def _render_plist(tmp_path, interval):
    """Run the REAL daemon/install.sh against a throwaway HOME, with launchctl and
    the binaries it probes stubbed out, and return the plist it rendered."""
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "maestro" / "_assets" / "daemon" / "install.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    for name in ("launchctl", "maestro", "claude"):
        stub = fake_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    fake_home = tmp_path / "fakehome"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["MAESTRO_HOME"] = str(tmp_path / "mhome")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    p = subprocess.run([str(script), "up", "--interval", str(interval)],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    plist = fake_home / "Library" / "LaunchAgents" / "com.maestro.dispatcher.plist"
    assert plist.exists(), p.stderr
    return plist, p


def test_install_script_renders_pinned_throttle_and_no_keepalive(tmp_path):
    """The rendered plist must pin ThrottleInterval to the same value as
    StartInterval (rather than inheriting launchd's undeclared 10s default) and
    must declare KeepAlive false — a crashed-only KeepAlive turns a dispatcher that
    dies at import into a hot restart loop."""
    plist, _ = _render_plist(tmp_path, 900)
    text = plist.read_text()
    assert fleet._interval_from_plist(plist) == 900
    assert fleet._throttle_from_plist(plist) == 900
    assert "<key>KeepAlive</key>" in text
    assert re.search(r"<key>KeepAlive</key>\s*<false/>", text)
    assert "@MAESTRO_HOME@" not in text and "@PATH@" not in text  # fully substituted


def test_install_script_floors_a_dangerous_interval(tmp_path):
    """`install.sh up --interval 10` — the direct-shell path that bypasses
    fleet.up — must refuse to render a 10s cadence."""
    plist, proc = _render_plist(tmp_path, 10)
    assert fleet._interval_from_plist(plist) == 60
    assert fleet._throttle_from_plist(plist) == 60
    assert "below the 60s floor" in proc.stderr


# --- pause / resume kill switch (T-15) --------------------------------------

def test_pause_writes_since_until_reason(home):
    fleet.pause(home, until=12345.0, reason="runaway")
    raw = store.read_json(fleet.pause_path(home), None)
    assert raw is not None
    assert raw["until"] == 12345.0
    assert raw["reason"] == "runaway"
    assert raw["since"] is not None


def test_pause_state_none_when_unpaused(home):
    assert fleet.pause_state(home, store.now_epoch()) is None


def test_pause_state_paused_with_no_until(home):
    fleet.pause(home, reason="indefinite")
    state = fleet.pause_state(home, store.now_epoch())
    assert state is not None
    assert state["reason"] == "indefinite"
    assert state["until"] is None


def test_pause_is_idempotent_and_reports_prior_state(home):
    fleet.pause(home, reason="first")
    out = fleet.pause(home, reason="second")
    assert out["reason"] == "second"
    assert out["previous"]["reason"] == "first"
    state = fleet.pause_state(home, store.now_epoch())
    assert state["reason"] == "second"


def test_resume_removes_pause_file(home):
    fleet.pause(home, reason="x")
    out = fleet.resume(home)
    assert out["resumed"] is True
    assert out["was_paused"] is True
    assert not fleet.pause_path(home).exists()
    assert fleet.pause_state(home, store.now_epoch()) is None


def test_resume_on_unpaused_home_is_a_noop(home):
    out = fleet.resume(home)
    assert out["resumed"] is True
    assert out["was_paused"] is False


def test_pause_state_fails_safe_on_corrupt_json(home):
    """File existence alone arms the switch, even with un-parseable JSON."""
    store.atomic_write(fleet.pause_path(home), "{not valid json::")
    state = fleet.pause_state(home, store.now_epoch())
    assert state is not None


def test_pause_state_fails_safe_on_garbage_until(home):
    """An unparseable `until` must not be treated as expired."""
    store.atomic_write(fleet.pause_path(home), '{"since": 1, "until": "garbage", "reason": "x"}')
    state = fleet.pause_state(home, store.now_epoch())
    assert state is not None
    assert state["until"] == "garbage"


def test_pause_state_auto_resumes_past_until(home):
    now = store.now_epoch()
    fleet.pause(home, until=now - 10, reason="expired")
    assert fleet.pause_state(home, now) is None
    assert not fleet.pause_path(home).exists()


def test_pause_state_stays_paused_before_until(home):
    now = store.now_epoch()
    fleet.pause(home, until=now + 3600, reason="future")
    state = fleet.pause_state(home, now)
    assert state is not None
    state2 = fleet.pause_state(home, now + 10)
    assert state2 is not None


def test_status_reports_paused(home):
    def run(cmd, **kw):
        return FakeProc(0, "")

    fleet.pause(home, until=999999999999.0, reason="testing")
    res = fleet.status(home, run=run, plist="/nonexistent")
    assert res["paused"] is True
    assert res["pause_reason"] == "testing"
    assert res["pause_until"] == 999999999999.0


def test_status_reports_not_paused(home):
    def run(cmd, **kw):
        return FakeProc(0, "")

    res = fleet.status(home, run=run, plist="/nonexistent")
    assert res["paused"] is False


# --- real CLI round-trip (T-15 ACs) -----------------------------------------

def test_cli_fleet_pause_resume_round_trip(home):
    import json

    from maestro import cli

    def _run(args):
        import io
        import sys
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = cli.main(["--home", str(home)] + args)
        finally:
            sys.stdout = old
        return rc, json.loads(buf.getvalue())

    rc, out = _run(["fleet", "pause", "--for", "2h", "--reason", "runaway"])
    assert rc == 0
    assert out["reason"] == "runaway"
    raw = store.read_json(fleet.pause_path(home), None)
    assert raw is not None and raw["reason"] == "runaway"

    # --until accepted as both a bare epoch and an ISO-8601 string.
    rc, out = _run(["fleet", "pause", "--until", "9999999999", "--reason", "epoch"])
    assert rc == 0 and out["until"] == 9999999999.0

    rc, out = _run(["fleet", "pause", "--until", "2099-01-01T00:00:00", "--reason", "iso"])
    assert rc == 0
    from datetime import datetime
    assert out["until"] == datetime.fromisoformat("2099-01-01T00:00:00").timestamp()

    # Idempotent re-arm reports the prior state.
    rc, out = _run(["fleet", "pause", "--reason", "again"])
    assert rc == 0 and out["previous"]["reason"] == "iso"

    rc, out = _run(["fleet", "resume"])
    assert rc == 0 and out["resumed"] is True
    assert not fleet.pause_path(home).exists()

    # resume on an already-unpaused home still exits 0.
    rc, out = _run(["fleet", "resume"])
    assert rc == 0


def test_cli_dispatch_json_includes_paused(home):
    import io
    import json
    import sys

    from maestro import cli

    fleet.pause(home, reason="cli check")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    finally:
        sys.stdout = old
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["paused"] is True


def test_cli_fleet_status_reports_paused(home):
    import io
    import json
    import sys

    from maestro import cli

    fleet.pause(home, until=store.now_epoch() + 3600, reason="status check")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["--home", str(home), "fleet", "status"])
    finally:
        sys.stdout = old
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out["paused"] is True
    assert out["pause_reason"] == "status check"
    assert out["pause_until"] is not None


def test_projection_workstate_carries_paused_banner(home):
    from maestro import projection

    fleet.pause(home, reason="board frozen")
    written = projection.write(home)
    assert "WORKSTATE.md" in written
    text = (home / "derived" / "WORKSTATE.md").read_text()
    assert "paused" in text.lower()
    assert "board frozen" in text


def test_projection_workstate_no_banner_when_unpaused(home):
    from maestro import projection

    projection.write(home)
    text = (home / "derived" / "WORKSTATE.md").read_text()
    assert "paused" not in text.lower()
