"""OC-2: fail-closed spawn preflight for a non-claude runner, probed once per
sweep (cached), inserted before `sessions.spawn`. Three outcomes: binary
missing/unexecutable or daemon unreachable are TRANSIENT -- a group-level skip
modelled on `repo_blocked` (a `DispatchReport` blocker, no event, no
attempts-ledger spend); model absent or not tool-capable is PERMANENT --
`ops.ask` with a stable qid, same shape as the existing `runner_unregistered`
branch, so it lands in NEEDS-YOU instead of sleeping forever. Never falls
through to a claude spawn on any branch.

`_REGISTERED_RUNNERS` (RF-2) is `{"claude"}` today -- no real second
`SessionManager` delegate exists yet, so every test here monkeypatches it to
admit a fake non-claude runner name past that earlier gate and into OC-2's new
preflight code, exactly like `test_ollama_health.py` fakes the ollama
transport rather than depending on a real daemon.
"""
from __future__ import annotations

import io
import json
import shutil
import sys

from maestro import cli, dispatcher as disp, event_log, health, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

RUNNER = "opencode"


def _sweep(home):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(home), "doctor"])
    finally:
        sys.stdout = old
    return code, json.loads(buf.getvalue())


def _register(monkeypatch, *names):
    monkeypatch.setattr(disp, "_REGISTERED_RUNNERS", frozenset({"claude", *names}))


def _enable(cfg, *names):
    # OC-3: the board-wide `runner_enabled` kill switch (default: claude only)
    # sits in front of OC-2's own preflight -- tests exercising THAT preflight
    # (this file) must flip it on for the runner under test, same as
    # `_register` admits the name past the earlier `_REGISTERED_RUNNERS` gate.
    cfg.runner_enabled = ["claude", *names]


def _seed(home, key, *, phase=Phase.IMPLEMENTING, runner=None, runner_model=None, approval_tier=0):
    extra = ""
    if runner:
        extra += f"runner: {runner}\n"
    if runner_model:
        extra += f"runner_model: {runner_model}\n"
    spec = f"# {key}\napproval_tier: {approval_tier}\n{extra}dependsOn: []\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _events_path(home, key):
    return home / "events" / f"{key}.jsonl"


def _counting_probe(result):
    calls = []

    def fn(runner):
        calls.append(runner)
        return result
    fn.calls = calls
    return fn


# --- AC1/AC6: daemon unreachable -> transient group-level skip, no event, no
# attempts spend, sibling still spawns, DispatchReport names the runner -------

def test_daemon_unreachable_skips_no_event_no_attempts_sibling_still_spawns(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "OK-1", phase=Phase.READY)
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    assert "OK-1" in report.spawned
    assert probe.calls == [RUNNER]

    events = event_log.read(home, "BAD-1")
    assert len(events) == 2  # unchanged: just the seeded TicketCreated + PhaseChanged
    assert not any(e["type"] == "Failed" for e in events)

    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.IMPLEMENTING.value  # unchanged

    attempts = store.read_json(home / "derived" / ".spawn_attempts.json", {}) or {}
    assert "BAD-1" not in attempts

    assert RUNNER in report.runner_blockers  # names the runner as the blocker key
    assert report.runner_blockers[RUNNER]  # non-empty reason


def test_daemon_unreachable_never_spawns_the_key_under_claude(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})
    sessions = DryRunSessions()

    disp.dispatch(cfg, sessions, now=1000, runner_probe=probe)

    assert "BAD-1" not in {s[0] for s in sessions.spawned}


# --- "probe once per sweep, cached -- never per key" (spec Notes) -----------

def test_two_keys_sharing_a_runner_probe_it_once_not_twice(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    _seed(home, "BAD-2", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    assert "BAD-2" not in report.spawned
    assert probe.calls == [RUNNER]  # one call total, not one per key


# --- AC2: ten consecutive sweeps under that condition -> zero spawns, zero
# Failed events -------------------------------------------------------------

def test_ten_consecutive_sweeps_daemon_unreachable_zero_spawns_zero_failed(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})

    spawned_total = 0
    for i in range(10):
        report = disp.dispatch(cfg, DryRunSessions(), now=1000 + i, runner_probe=probe)
        spawned_total += len(report.spawned)

    assert spawned_total == 0
    events = event_log.read(home, "BAD-1")
    assert not any(e["type"] == "Failed" for e in events)
    assert len(events) == 2  # still just the two seeded events


# --- AC3: model absent -> permanent, ops.ask, single QuestionAsked, no dupe
# on a second sweep -----------------------------------------------------------

def test_model_absent_asks_with_stable_qid_awaiting_human_no_failure_count_burn(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="ghost-model:1b")
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    events = event_log.read(home, "BAD-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert asked[0]["payload"]["qid"] == "runner-model-BAD-1-opencode-ghost-model:1b"
    assert not any(e["type"] == "Failed" for e in events)

    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.failure_count == 0

    # Second sweep: the ticket is now sleeping (awaiting-human) -- no duplicate
    # question gets appended.
    disp.dispatch(cfg, DryRunSessions(), now=1001, runner_probe=probe)
    events_after = event_log.read(home, "BAD-1")
    asked_after = [e for e in events_after if e["type"] == "QuestionAsked"]
    assert len(asked_after) == 1


def test_model_absent_never_spawns_the_key_under_claude(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="ghost-model:1b")
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})
    sessions = DryRunSessions()

    disp.dispatch(cfg, sessions, now=1000, runner_probe=probe)

    assert "BAD-1" not in {s[0] for s in sessions.spawned}


# --- AC4: model present but not tool-capable -> same treatment, capability
# named in the question text --------------------------------------------------

def test_model_not_tool_capable_names_the_capability_in_the_question(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="embed-only:7b")
    probe = _counting_probe({
        "binary_ok": True,
        "models": [{"name": "embed-only:7b", "capabilities": ["completion"]}],
        "daemon_reason": None,
    })

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    events = event_log.read(home, "BAD-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert "tools" in asked[0]["payload"]["text"]

    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.failure_count == 0


# --- AC5: binary probe False -> skipped, NO event of any type (byte
# compared), real `maestro doctor` warns naming it ----------------------------

def test_binary_missing_appends_no_event_byte_compared(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _enable(cfg, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    before = _events_path(home, "BAD-1").read_bytes()
    probe = _counting_probe({"binary_ok": False, "models": None, "daemon_reason": None})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    after = _events_path(home, "BAD-1").read_bytes()
    assert after == before
    assert RUNNER in report.runner_blockers


def test_binary_missing_real_doctor_warns_naming_the_runner(home, monkeypatch):
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    code, out = _sweep(home)

    assert code == 0  # WARN-only, never blocks
    check = next(c for c in out["checks"] if c["name"] == "runner_binary")
    assert check["status"] == "warn"
    assert "BAD-1" in check["tickets"]
    assert check["tickets"]["BAD-1"]["runner"] == RUNNER


def test_check_runner_binary_registered_in_doctor(cfg):
    names = {r["name"] for r in health.run_checks(cfg, 1000)}
    assert "runner_binary" in names


def test_check_runner_binary_ok_when_binary_present(home, cfg):
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    result = health.check_runner_binary(cfg, 1000, which=lambda name: f"/usr/local/bin/{name}")
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_check_runner_binary_ok_when_no_ticket_uses_a_non_claude_runner(home, cfg):
    _seed(home, "OK-1", phase=Phase.READY)

    def boom(name):
        raise AssertionError("must not call `which` when nothing needs checking")
    result = health.check_runner_binary(cfg, 1000, which=boom)
    assert result["status"] == "ok"
    assert result["tickets"] == {}


# --- AC7: a home with no `runner:` fields calls the probe zero times --------

def test_no_runner_fields_board_calls_probe_zero_times(home, cfg):
    _seed(home, "T-1", phase=Phase.READY)
    _seed(home, "T-2", phase=Phase.IMPLEMENTING)  # no runner: override either
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})

    disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert probe.calls == []
