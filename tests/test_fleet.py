"""`maestro fleet` — tested against a fake launchctl / install script (no real system)."""
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
