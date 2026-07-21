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

    script = Path(__file__).resolve().parents[1] / "daemon" / "install.sh"
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
