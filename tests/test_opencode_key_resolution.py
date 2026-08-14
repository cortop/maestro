"""T-66: an opencode reconciler must resolve its own ticket key by
construction -- from the spawned argv (see `OpencodeCliSessions.spawn`,
sessions.py) -- never by enumerating the board and guessing.

These tests spawn a REAL (stub) `opencode` binary through a REAL dispatcher
sweep, exactly like `test_runner_opencode_concurrency.py`'s fast-exiting-stub
tests -- never a mocked `subprocess.Popen`. The stub itself drives the REAL
`maestro` CLI (via `sys.executable -m maestro.cli`, the same interpreter this
test runs under, so the editable install is importable) with the ticket key
it received in argv, and logs the real output -- proving the key that reaches
a "first key-bearing maestro call" is the one the dispatcher intended, read
back from a session log, not asserted against a mock.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from maestro import claims, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import OpencodeCliSessions, RoutingSessions
from maestro.statemachine import Phase

from test_runner_opencode_concurrency import _TOOL_CAPABLE_PROBE

RUNNER = "opencode"

_STUB_OPENCODE = """\
#!{python}
# Stub opencode: the LAST argv element is the ticket key (see
# OpencodeCliSessions.spawn) -- drive the real maestro CLI with it, exactly
# like a real reconcile command's preamble does, and log the output keyed by
# whatever key we actually received (never a hardcoded/guessed one).
import os
import subprocess
import sys

key = sys.argv[-1]
home = os.environ["STUB_MAESTRO_HOME"]
log_dir = os.environ["STUB_LOG_DIR"]
log_path = os.path.join(log_dir, key + ".log")

calls = [
    [sys.executable, "-m", "maestro.cli", "--home", home, "env", "--key", key],
    [sys.executable, "-m", "maestro.cli", "--home", home, "observe-spec", key],
    [sys.executable, "-m", "maestro.cli", "--home", home, "snapshot", key],
]
with open(log_path, "w") as f:
    for c in calls:
        f.write("$ " + " ".join(c) + "\\n")
        r = subprocess.run(c, capture_output=True, text=True)
        f.write(r.stdout)
        f.write(r.stderr)
        f.write("\\n")
sys.exit(0)
"""


def _install_stub_opencode(tmp_path, monkeypatch):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(_STUB_OPENCODE.format(python=sys.executable))
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _seed(home, key, *, phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b"):
    spec = (f"# {key}\napproval_tier: 0\nrunner: {runner}\n"
            f"runner_model: {runner_model}\ndependsOn: []\n")
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _wait_and_read_log(home, log_dir, key, timeout=10.0):
    """Block on the real spawned process (this test IS its OS parent, since
    `sessions.spawn` -> `subprocess.Popen` ran in-process) so the log is
    guaranteed complete before we read it -- no sleep-and-hope polling."""
    deadline = time.time() + timeout
    pid = None
    while time.time() < deadline:
        claim = claims.read_claim(home, key)
        if claim and claim.get("pid"):
            pid = claim["pid"]
            break
        time.sleep(0.05)
    assert pid is not None, f"{key} never got a claim/pid"
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass  # already reaped
    log_path = Path(log_dir) / f"{key}.log"
    for _ in range(50):
        if log_path.exists():
            break
        time.sleep(0.05)
    assert log_path.exists(), f"no session log written for {key}"
    return log_path.read_text()


# --- AC1: a real spawn's first key-bearing maestro call carries the correct
# key, read back from the session log, not a mock ----------------------------

def test_real_spawn_first_key_bearing_call_carries_the_correct_key(
        home, cfg, tmp_path, monkeypatch):
    _install_stub_opencode(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("STUB_MAESTRO_HOME", str(home))
    monkeypatch.setenv("STUB_LOG_DIR", str(log_dir))

    cfg.runner_enabled = ["claude", RUNNER]
    _seed(home, "T-77")
    sessions = RoutingSessions({"opencode": OpencodeCliSessions(home, capture_session_logs=False)})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert "T-77" in report.spawned

    text = _wait_and_read_log(home, log_dir, "T-77")
    # The first key-bearing call (`env --key T-77`) resolved the real binding
    # -- not an error, not some other ticket's binding.
    assert '"repo":' in text
    # snapshot's own JSON echoes back the key it resolved -- the strongest
    # possible proof the RIGHT key reached the command, not a guessed one.
    assert '"key": "T-77"' in text
    # Never enumerated the board looking for itself.
    assert "find" not in text.lower()
    assert "grep" not in text.lower()


# --- AC3: two same-phase tickets, one real sweep, each gets its OWN distinct
# key -- neither session's log shows board enumeration -----------------------

def test_two_same_phase_tickets_each_spawn_with_their_own_distinct_key(
        home, cfg, tmp_path, monkeypatch):
    _install_stub_opencode(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("STUB_MAESTRO_HOME", str(home))
    monkeypatch.setenv("STUB_LOG_DIR", str(log_dir))

    cfg.runner_enabled = ["claude", RUNNER]
    cfg.provider_config = {"runner": {"opencode": {"concurrency": 2}}}
    _seed(home, "OC-10")
    _seed(home, "OC-11")
    sessions = RoutingSessions({"opencode": OpencodeCliSessions(home, capture_session_logs=False)})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert set(report.spawned) == {"OC-10", "OC-11"}

    text_10 = _wait_and_read_log(home, log_dir, "OC-10")
    text_11 = _wait_and_read_log(home, log_dir, "OC-11")

    assert '"key": "OC-10"' in text_10
    assert '"key": "OC-11"' in text_11
    for text in (text_10, text_11):
        assert "find" not in text.lower()
        assert "grep" not in text.lower()


# --- AC2: `maestro env --key ""` (and other key-taking verbs) hard-error ----

def test_env_with_empty_key_flag_exits_nonzero(home, capsys):
    rc = cli_main(["--home", str(home), "env", "--key", ""])
    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err.lower()


def test_env_with_no_key_flag_still_works_board_wide(home, capsys):
    """Sanity: only an EXPLICIT empty string is rejected -- omitting `--key`
    entirely (`None`) keeps returning the board-wide config, unchanged."""
    rc = cli_main(["--home", str(home), "env"])
    assert rc == 0


def test_snapshot_with_empty_key_exits_nonzero(home, capsys):
    rc = cli_main(["--home", str(home), "snapshot", ""])
    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err.lower()


def test_dispatch_key_filter_with_empty_key_exits_nonzero(home, capsys):
    rc = cli_main(["--home", str(home), "dispatch", "--dry-run", "--key", ""])
    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err.lower()
