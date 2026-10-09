"""T-176: `maestro stop KEY` -- SIGTERM one confirmed live session, log untouched."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time

import pytest

from conftest import seed_ticket
from maestro import claims, cli, dispatcher as disp, event_log, snapshot as snap_mod

SLEEP = "import time; time.sleep(60)"
DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"


def _cli(home, *argv):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), *argv])
    finally:
        sys.stdout = old
    return code, buf.getvalue()


@pytest.fixture
def spawn():
    procs = []

    def _spawn(code=SLEEP, *, reap=True):
        p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                             stdout=subprocess.PIPE if code is DEAF else None)
        if code is DEAF:
            p.stdout.readline()  # handler installed before we signal
        if reap:  # production: launchd reaps the orphan; here we are the parent
            threading.Thread(target=p.wait, daemon=True).start()
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        p.wait(timeout=5)
        if p.stdout:
            p.stdout.close()


def _events(home, key):
    return [e["type"] for e in event_log.read(home, key)]


def test_stop_kills_confirmed_session_without_failed_event(home, spawn):
    seed_ticket(home, "T-3", "stop me")
    before = _events(home, "T-3")
    fc = snap_mod.load(home, "T-3").failure_count
    proc = spawn()
    claims.write_claim(home, "T-3", proc.pid, "reconcile-T-3")
    code, out = _cli(home, "stop", "T-3", "--wait", "5")
    assert code == 0, out
    res = json.loads(out)
    assert res["pid"] == proc.pid and res["verdict"] == "confirmed" and res["stopped"] is True
    assert not claims.pid_alive(proc.pid)
    assert _events(home, "T-3") == before and "Failed" not in _events(home, "T-3")
    assert snap_mod.load(home, "T-3").failure_count == fc
    assert "T-3" not in claims.active_keys(home)
    assert claims.read_claim(home, "T-3") is None


def test_stop_refuses_unconfirmed_claim(home, spawn):
    seed_ticket(home, "T-3", "stop me")
    proc = spawn()
    time.sleep(1.1)  # ps etime has 1s resolution
    claims.write_claim(home, "T-3", proc.pid, "reconcile-T-3")
    claim = claims.read_claim(home, "T-3")
    claim["epoch"] -= 3600  # claim predates the process -> pid reuse
    claims.claim_path(home, "T-3").write_text(json.dumps(claim))
    raw = claims.claim_path(home, "T-3").read_text()
    code, out = _cli(home, "stop", "T-3")
    assert code != 0 and json.loads(out)["verdict"] == "denied"
    assert proc.poll() is None
    assert claims.claim_path(home, "T-3").read_text() == raw
    # no claim at all
    claims.release(home, "T-3")
    code, out = _cli(home, "stop", "T-3")
    assert code != 0 and json.loads(out)["stopped"] is False
    assert proc.poll() is None


@pytest.mark.parametrize("kind", ["testrun", "restack"])
def test_stop_refuses_dispatcher_owned_claims(home, spawn, kind):
    seed_ticket(home, "T-3", "stop me")
    proc = spawn()
    claims.write_claim(home, "T-3", proc.pid, "x", kind=kind)
    code, out = _cli(home, "stop", "T-3")
    assert code != 0 and "dispatcher-owned" in json.loads(out)["outcome"]
    assert proc.poll() is None
    assert claims.read_claim(home, "T-3") is not None


def test_stop_reports_survivor(home, spawn):
    seed_ticket(home, "T-3", "stop me")
    proc = spawn(DEAF)
    claims.write_claim(home, "T-3", proc.pid, "reconcile-T-3")
    code, out = _cli(home, "stop", "T-3", "--wait", "1")
    assert code != 0
    res = json.loads(out)
    assert res["stopped"] is False and "still running" in res["outcome"]
    assert proc.poll() is None
    assert claims.read_claim(home, "T-3") is not None


def test_stop_refuses_non_group_leader(home, child_process):
    seed_ticket(home, "T-3", "stop me")
    claims.write_claim(home, "T-3", child_process.pid, "reconcile-T-3")
    code, out = _cli(home, "stop", "T-3")
    assert code != 0 and "process group" in json.loads(out)["outcome"]
    assert child_process.poll() is None


def test_stop_is_human_only(capsys):
    assert "stop" not in disp.AGENT_TOOL_VERBS
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    line = next(l for l in help_text.splitlines() if l.strip().startswith("stop "))
    assert "[human]" in line
