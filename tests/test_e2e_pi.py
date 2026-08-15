"""PI-8: end-to-end flow proofs -- AC10 (`maestro create --runner pi
--runner-model <tag>` through real sweeps with real stub executables: a
pi-run phase, then a claude-run phase, then `awaiting-ci`, the runner
asserted per spawn) and AC11 (a board with no pi ticket is byte-identical
whether or not the pi backend is registered).
"""
from __future__ import annotations

import os
import time

from maestro import cli, dispatcher as disp, event_log, projection, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions, RoutingSessions, list_sessions
from maestro.statemachine import Phase

from test_runner_preflight import _seed

_STUB_EXIT_0 = "#!/bin/sh\nexit 0\n"


def _install_stub(tmp_path, monkeypatch, name):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(_STUB_EXIT_0)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return stub


def _fake_probe_table(cfg):
    return lambda runner: {"binary_ok": True, "models": [{"model": "glm-5.2"}],
                            "daemon_reason": None}


# --- AC10: real `create --runner pi` -> real sweeps, stub executables --------

def test_end_to_end_create_runner_pi_through_sweeps_to_awaiting_ci(
        home, tmp_path, monkeypatch):
    _install_stub(tmp_path, monkeypatch, "pi")
    _install_stub(tmp_path, monkeypatch, "claude")
    (home / "config.toml").write_text(
        "[maestro]\nmin_spawn_interval = 0\n"
        "runner_enabled = [\"claude\", \"pi\"]\n\n"
        "[runner.pi]\nphases = [\"researching\"]\n",
        encoding="utf-8")
    # No repo_path configured -- _worker_cwd falls back to `home` itself (a
    # real dir), so a real Popen spawn needs no git worktree machinery --
    # orthogonal to what this AC proves (runner routing per phase, over a
    # REAL sweep/CLI).
    monkeypatch.setattr(disp, "_make_default_runner_probe", _fake_probe_table)

    # --json mints synchronously (bypasses the _new inbox) -- the spec exists
    # the moment this call returns, no separate mint_new_tickets step needed.
    rc = cli.main(["--home", str(home), "create", "PI-8 e2e", "--key", "T-1",
                   "--tier", "0", "--runner", "pi", "--runner-model", "glm-5.2",
                   "--json", "--no-nudge"])
    assert rc == 0
    spec_text = store.spec_path(home, "T-1").read_text()
    assert "runner: pi" in spec_text
    assert "runner_model: glm-5.2" in spec_text

    # Jump straight to `researching` -- the phase [runner.pi] admits pi to --
    # triage/approval is orthogonal to what this AC proves.
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.RESEARCHING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    sessions_after_research = list_sessions(home, "T-1")
    assert len(sessions_after_research) == 1
    assert sessions_after_research[0]["format"] == "pi"  # T-58: pi's own log identity
    time.sleep(0.3)  # let the fast-exiting stub actually exit before the next sweep

    # Hand off to `implementing` -- pi is NOT admitted there by default (or by
    # this test's own config), so this spawn must go to claude.
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    sessions_after_impl = list_sessions(home, "T-1")
    assert len(sessions_after_impl) == 2
    assert sessions_after_impl[0]["format"] != "pi"  # newest -- claude's own format

    event_log.append(home, "T-1", "PrOpened",
                     {"number": 1, "url": "https://x/pull/1", "draft": True}, actor="r")
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "qa"])
    assert rc == 0

    # This create-minted spec has no `## Acceptance criteria` checkboxes (out
    # of scope for what this AC proves) -- hand off the last hop directly,
    # same as test_e2e_opencode.py's own AC8 counterpart.
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0

    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# --- AC11: no pi ticket -> byte-identical whether or not the backend is
# registered -------------------------------------------------------------

def test_no_pi_ticket_byte_identical_whether_backend_registered(tmp_path, monkeypatch):
    fixed_iso = "2026-01-01T00:00:00+00:00"
    fixed_epoch = 1_700_000_000.0
    monkeypatch.setattr(store, "iso_now", lambda: fixed_iso)
    monkeypatch.setattr(store, "now_epoch", lambda: fixed_epoch)

    def _build(home):
        for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
            (home / d).mkdir(parents=True, exist_ok=True)
        cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
        _seed(home, "T-1", phase=Phase.READY)                    # no runner: override
        _seed(home, "T-2", phase=Phase.IMPLEMENTING)              # no runner: override
        return cfg

    home_registered = tmp_path / "registered"
    home_registered.mkdir()
    cfg_registered = _build(home_registered)
    claude_registered = DryRunSessions()
    sessions_registered = RoutingSessions(
        {"claude": claude_registered, "pi": DryRunSessions()})
    report_registered = disp.dispatch(cfg_registered, sessions_registered, now=1000)
    projection.write(home_registered)

    home_unregistered = tmp_path / "unregistered"
    home_unregistered.mkdir()
    cfg_unregistered = _build(home_unregistered)
    claude_unregistered = DryRunSessions()
    sessions_unregistered = RoutingSessions({"claude": claude_unregistered})
    report_unregistered = disp.dispatch(cfg_unregistered, sessions_unregistered, now=1000)
    projection.write(home_unregistered)

    def _normalize(spawned, home):
        return [t[:2] + (t[2].replace(str(home), "<home>"),) + t[3:] for t in spawned]

    assert report_registered.spawned == report_unregistered.spawned
    assert (_normalize(claude_registered.spawned, home_registered)
            == _normalize(claude_unregistered.spawned, home_unregistered))
    for fname in ("WORKSTATE.md", "NEEDS-YOU.md"):
        a = (home_registered / "derived" / fname).read_bytes()
        b = (home_unregistered / "derived" / fname).read_bytes()
        assert a == b, f"{fname} differs with pi registered vs unregistered"
