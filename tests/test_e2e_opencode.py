"""OC-4: end-to-end flow proofs -- AC5 (same ticket's `implementing` spawn goes
to opencode, its `qa` spawn goes to claude), AC8 (`maestro create --runner
opencode` through real sweeps to `awaiting-ci`, runner asserted per spawn via
real stub executables), and AC9 (a board with no opencode ticket is
byte-identical whether or not the opencode backend is registered).
"""
from __future__ import annotations

import os
import time

from maestro import cli, dispatcher as disp, event_log, projection, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions, RoutingSessions, list_sessions
from maestro.statemachine import Phase

from test_runner_preflight import RUNNER, _counting_probe, _seed

_TOOL_CAPABLE_PROBE = _counting_probe({
    "binary_ok": True,
    "models": [{"name": "a:1b", "capabilities": ["tools"]}],
    "daemon_reason": None,
})


# --- AC5: same ticket, implementing -> opencode, qa -> claude ---------------

def test_same_ticket_implementing_spawns_opencode_qa_spawns_claude(home, cfg):
    cfg.runner_enabled = ["claude", RUNNER]
    cfg.min_spawn_interval = 0
    _seed(home, "T-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    claude_arm = DryRunSessions()
    opencode_arm = DryRunSessions()
    sessions = RoutingSessions({"claude": claude_arm, "opencode": opencode_arm})

    report = disp.dispatch(cfg, sessions, now=1000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert "T-1" in report.spawned
    assert [s[0] for s in opencode_arm.spawned] == ["T-1"]
    assert claude_arm.spawned == []

    # Hand off to qa (the spec's `runner: opencode` line is untouched -- RF-2's
    # own rule: only the `implementing` spawn ever honors it).
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    report2 = disp.dispatch(cfg, sessions, now=2000, runner_probe=_TOOL_CAPABLE_PROBE)
    assert "T-1" in report2.spawned
    assert [s[0] for s in claude_arm.spawned] == ["T-1"]
    assert [s[0] for s in opencode_arm.spawned] == ["T-1"]  # unchanged from before


# --- AC8: real `create --runner opencode` -> real sweeps, stub executables --

_STUB_EXIT_0 = "#!/bin/sh\nexit 0\n"


def _install_stub(tmp_path, monkeypatch, name):
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(_STUB_EXIT_0)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return stub


def test_end_to_end_create_runner_opencode_through_sweeps_to_awaiting_ci(
        home, tmp_path, monkeypatch):
    _install_stub(tmp_path, monkeypatch, "opencode")
    _install_stub(tmp_path, monkeypatch, "claude")
    (home / "config.toml").write_text(
        "[maestro]\nmin_spawn_interval = 0\nrunner_enabled = [\"claude\", \"opencode\"]\n",
        encoding="utf-8")
    # No repo_path configured -- _worker_cwd falls back to `home` itself (a real
    # dir), so a real Popen spawn needs no git worktree machinery -- orthogonal
    # to what this AC proves (runner routing per phase, over a REAL sweep/CLI).
    monkeypatch.setattr(disp, "_default_runner_probe", lambda runner: {
        "binary_ok": True, "models": [{"name": "a:1b", "capabilities": ["tools"]}],
        "daemon_reason": None})

    # --json mints synchronously (bypasses the _new inbox) -- the spec exists
    # the moment this call returns, no separate mint_new_tickets step needed.
    rc = cli.main(["--home", str(home), "create", "OC-4 e2e", "--key", "T-1",
                   "--tier", "0", "--runner", "opencode", "--runner-model", "a:1b",
                   "--json", "--no-nudge"])
    assert rc == 0
    spec_text = store.spec_path(home, "T-1").read_text()
    assert "runner: opencode" in spec_text
    assert "runner_model: a:1b" in spec_text

    # Jump straight to implementing -- triage/approval/worktree-adoption is
    # orthogonal to what this AC proves.
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value}, actor="r")
    snap_mod.rebuild(home, "T-1")

    rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    sessions_after_impl = list_sessions(home, "T-1")
    assert len(sessions_after_impl) == 1
    assert sessions_after_impl[0]["format"] == "opencode"  # RF-3: opencode's own log identity
    time.sleep(0.3)  # let the fast-exiting stub actually exit before the next sweep

    event_log.append(home, "T-1", "PrOpened",
                     {"number": 1, "url": "https://x/pull/1", "draft": True}, actor="r")
    # No --requeue here (unlike the real skill's own call) -- this test drives
    # real wall-clock sweeps back-to-back, so a requeue timer would just make
    # the ticket sleep past the very next assertion; orthogonal to this AC.
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "qa"])
    assert rc == 0

    rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    sessions_after_qa = list_sessions(home, "T-1")
    assert len(sessions_after_qa) == 2
    assert sessions_after_qa[0]["format"] != "opencode"  # newest -- claude's own format

    # This create-minted spec has no `## Acceptance criteria` checkboxes (out
    # of scope for what this AC proves -- qa-verdict's AC bookkeeping is
    # exercised in full by test_qa_roundtrip.py), so hand off the last hop the
    # same way the earlier implementing->qa hop above was driven -- directly.
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0

    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# --- AC9: no opencode ticket -> byte-identical whether or not the backend is
# registered ------------------------------------------------------------------

def test_no_opencode_ticket_byte_identical_whether_backend_registered(tmp_path, monkeypatch):
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
        {"claude": claude_registered, "opencode": DryRunSessions()})
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
        # `cwd` (index 2) embeds the home's own tmp_path -- the two homes are
        # necessarily different directories, so strip that one home-specific
        # element before comparing; every other element must still match
        # byte-for-byte.
        return [t[:2] + (t[2].replace(str(home), "<home>"),) + t[3:] for t in spawned]

    assert report_registered.spawned == report_unregistered.spawned
    assert (_normalize(claude_registered.spawned, home_registered)
            == _normalize(claude_unregistered.spawned, home_unregistered))
    for fname in ("WORKSTATE.md", "NEEDS-YOU.md"):
        a = (home_registered / "derived" / fname).read_bytes()
        b = (home_unregistered / "derived" / fname).read_bytes()
        assert a == b, f"{fname} differs with opencode registered vs unregistered"
