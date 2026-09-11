"""OC-2: fail-closed spawn preflight for a non-claude runner, probed once per
sweep (cached), inserted before `sessions.spawn`. Three outcomes: binary
missing/unexecutable or daemon unreachable are TRANSIENT -- a group-level skip
modelled on `repo_blocked` (a `DispatchReport` blocker, no event, no
attempts-ledger spend); model absent or not tool-capable is PERMANENT --
`ops.ask` with a stable qid, same shape as the existing `runner_unregistered`
branch, so it lands in NEEDS-YOU instead of sleeping forever. Never falls
through to a claude spawn on any branch.

`_REGISTERED_RUNNERS` is `{"claude", "opencode"}` as of OC-4 -- every test here
still monkeypatches it (redundantly, now that "opencode" is real) so this file
stays independent of whatever runner names happen to be registered, exactly
like `test_ollama_health.py` fakes the ollama transport rather than depending
on a real daemon. This file exercises the preflight gate itself with plain
`DryRunSessions`, never a real `OpencodeCliSessions` spawn -- see
`test_opencode_sessions.py` for that.
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
    spec = (f"# {key}\napproval_tier: {approval_tier}\n{extra}dependsOn: []\n\n"
            "## Acceptance criteria\n- [ ] ok\n")
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


# --- T-61 (PI-9) AC7: check_runner_binary works unchanged for pi ---------------


def test_check_runner_binary_ok_when_pi_binary_present(home, cfg):
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner="pi", runner_model="glm-5.2")
    result = health.check_runner_binary(cfg, 1000, which=lambda name: f"/usr/local/bin/{name}")
    assert result["status"] == "ok"
    assert result["tickets"] == {}


def test_check_runner_binary_warns_when_pi_binary_absent(home, cfg):
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner="pi", runner_model="glm-5.2")
    result = health.check_runner_binary(cfg, 1000, which=lambda name: None)
    assert result["status"] == "warn"
    assert result["tickets"]["PI-1"]["runner"] == "pi"
    assert "not found on PATH" in result["tickets"]["PI-1"]["reason"]


# --- AC7: a home with no `runner:` fields calls the probe zero times --------

def test_no_runner_fields_board_calls_probe_zero_times(home, cfg):
    _seed(home, "T-1", phase=Phase.READY)
    _seed(home, "T-2", phase=Phase.IMPLEMENTING)  # no runner: override either
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})

    disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert probe.calls == []


# --- `[runner.<name>] bin`: resolve the runner the DAEMON sees ----------------
#
# The fault these cover: maestro built the LaunchAgent PATH from the maestro
# dir, the claude dir and the system dirs, with no notion that [runner.*]
# exists. A runner living anywhere else (a volta shim under $HOME) was
# unreachable from the daemon while resolving perfectly in the installing
# shell -- and the health check resolved against that same shell, so it
# reported "all resolve ok" straight through a total freeze.


def _installed_plist(tmp_path, path_value: str):
    """A minimal LaunchAgent plist carrying just an EnvironmentVariables PATH."""
    p = tmp_path / "com.maestro.dispatcher.plist"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
        "<key>EnvironmentVariables</key><dict>\n"
        f"<key>PATH</key><string>{path_value}</string>\n"
        "</dict>\n</dict></plist>\n", encoding="utf-8")
    return p


def _stub_binary(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_doctor_warns_when_the_runner_resolves_in_the_shell_but_not_the_daemon(
        home, cfg, tmp_path):
    """The exact freeze shape, and the one this check used to call healthy."""
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner="pi", runner_model="glm-5.2")
    shell_only = tmp_path / "shell-only"
    _stub_binary(shell_only, "pi")
    plist = _installed_plist(tmp_path, "/usr/bin:/bin")  # daemon cannot see it

    result = health.check_runner_binary(
        cfg, 1000, which=lambda name: str(shell_only / name), plist=plist)

    assert result["status"] == "warn"
    reason = result["tickets"]["PI-1"]["reason"]
    assert "resolves in this shell but NOT on the dispatcher's own PATH" in reason
    assert "[runner.pi] bin" in reason


def test_doctor_ok_once_the_runner_dir_is_on_the_daemon_path(home, cfg, tmp_path):
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner="pi", runner_model="glm-5.2")
    runner_dir = tmp_path / "volta" / "bin"
    _stub_binary(runner_dir, "pi")
    plist = _installed_plist(tmp_path, f"{runner_dir}:/usr/bin:/bin")

    result = health.check_runner_binary(
        cfg, 1000, which=lambda name: str(runner_dir / name), plist=plist)

    assert result["status"] == "ok"
    assert result["tickets"] == {}
    assert "the dispatcher's PATH" in result["detail"]


def test_doctor_warns_when_a_configured_bin_is_not_executable(home, cfg, tmp_path):
    """A `bin` that is set but wrong must report missing, not pass preflight
    and die at Popen."""
    _seed(home, "PI-1", phase=Phase.IMPLEMENTING, runner="pi", runner_model="glm-5.2")
    ghost = tmp_path / "nowhere" / "pi"
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{ghost}"\n', encoding="utf-8")
    from maestro import config as config_mod
    fresh = config_mod.load(str(cfg.home))

    result = health.check_runner_binary(fresh, 1000, which=lambda name: None)

    assert result["status"] == "warn"
    assert "is not executable" in result["tickets"]["PI-1"]["reason"]


def test_a_configured_bin_reaches_the_spawn_preflight(home, cfg, tmp_path):
    """`resolve_binary` is what the preflight's `binary_ok` consults, so a
    configured bin must satisfy it even when the bare name is nowhere on PATH
    -- otherwise the fix stops at config and the board stays frozen."""
    runner_dir = tmp_path / "volta" / "bin"
    _stub_binary(runner_dir, "pi")
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{runner_dir / "pi"}"\n', encoding="utf-8")
    from maestro import config as config_mod
    fresh = config_mod.load(str(cfg.home))

    assert disp.resolve_binary(fresh, "pi") == str(runner_dir / "pi")
    # Nothing named `definitely-not-installed` exists anywhere.
    assert disp.resolve_binary(fresh, "definitely-not-installed") is None


def test_launchd_path_carries_every_configured_runner_dir(cfg, tmp_path):
    """`fleet up` regenerates the plist, so the runner dir has to come from
    config -- otherwise each reinstall drops a hand-added entry again."""
    from maestro import config as config_mod, fleet
    runner_dir = tmp_path / "volta" / "bin"
    _stub_binary(runner_dir, "pi")
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{runner_dir / "pi"}"\n', encoding="utf-8")
    fresh = config_mod.load(str(cfg.home))

    path = fleet.launchd_path(fresh, maestro_bin="/opt/m/bin/maestro",
                              claude_bin="/opt/c/bin/claude")
    entries = path.split(":")
    assert str(runner_dir) in entries
    assert entries.index("/opt/m/bin") < entries.index(str(runner_dir))
    assert entries[-4:] == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def test_launchd_path_unchanged_when_no_runner_bin_is_configured(cfg):
    """Ships dark: a board configuring nothing gets the original three sources."""
    from maestro import fleet
    path = fleet.launchd_path(cfg, maestro_bin="/opt/m/bin/maestro",
                              claude_bin="/opt/c/bin/claude")
    assert path == ("/opt/m/bin:/opt/c/bin:/opt/homebrew/bin:/usr/local/bin"
                    ":/usr/bin:/bin")


def test_spawn_env_prepends_the_configured_runner_dir(cfg, tmp_path):
    """The reconciler's own PATH -- what lets the runner resolve the tools IT
    shells out for, which pinning argv[0] would not fix."""
    from maestro import config as config_mod, sessions
    runner_dir = tmp_path / "volta" / "bin"
    _stub_binary(runner_dir, "pi")
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{runner_dir / "pi"}"\n', encoding="utf-8")

    env = sessions._spawn_env(cfg.home, None)
    assert env["PATH"].split(":")[0] == str(runner_dir)
    assert env["MAESTRO_HOME"] == str(cfg.home)

    # And a board with no `bin` keeps its PATH byte-identical.
    (cfg.home / "config.toml").write_text("[runner.pi]\nprovider = \"zai\"\n",
                                          encoding="utf-8")
    import os
    assert sessions._spawn_env(cfg.home, None)["PATH"] == os.environ["PATH"]


def test_bin_must_be_named_after_the_runner(cfg, tmp_path):
    """Only the DIRECTORY reaches PATH and argv[0] stays the bare name, so a
    versioned filename would put its dir on PATH and then never be found."""
    from maestro import config as config_mod
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{tmp_path / "pi-1.2"}"\n', encoding="utf-8")
    try:
        config_mod.load(str(cfg.home))
        raise AssertionError("a mismatched basename must fail config.load")
    except store.MaestroError as e:
        assert "must be named 'pi'" in str(e)
        assert "Symlink it" in str(e)


def test_bin_rejects_characters_that_would_corrupt_the_plist(cfg, tmp_path):
    """install.sh substitutes the PATH with `sed -e "s#@PATH@#...#g"` into XML."""
    from maestro import config as config_mod
    for bad in ("/opt/we#ird/pi", "/opt/a&b/pi"):
        (cfg.home / "config.toml").write_text(
            f'[runner.pi]\nbin = "{bad}"\n', encoding="utf-8")
        try:
            config_mod.load(str(cfg.home))
            raise AssertionError(f"{bad!r} must fail config.load")
        except store.MaestroError as e:
            assert "would corrupt it" in str(e), str(e)


def test_every_pi_executor_resolves_through_the_configured_bin(cfg, tmp_path, monkeypatch):
    """Not just the spawn path: `doctor`'s pi checks, `maestro runners` and
    `set-runner` each shell their own `pi`. Leaving any of them on the invoking
    shell's PATH reproduces the false-green this whole change exists to end,
    one layer up -- a board where only the daemon's PATH is wrong would still
    be told everything resolves.
    """
    from maestro import config as config_mod, health, ops
    runner_dir = tmp_path / "volta" / "bin"
    _stub_binary(runner_dir, "pi")
    (cfg.home / "config.toml").write_text(
        f'[runner.pi]\nbin = "{runner_dir / "pi"}"\nversion = "9.9.9"\n', encoding="utf-8")
    fresh = config_mod.load(str(cfg.home))

    seen = {}

    def _recording_run(argv, **kw):
        seen["argv"] = argv
        seen["path"] = (kw.get("env") or {}).get("PATH")
        class P:
            returncode = 0
            stdout = "9.9.9"
            stderr = ""
        return P()

    health.check_pi_version(fresh, 1000, run=_recording_run)
    assert seen["argv"] == ["pi", "--version"]
    assert seen["path"] is not None, "check_pi_version shelled pi with no env"
    assert seen["path"].split(":")[0] == str(runner_dir)

    # suggest_acs shells `claude`, which is equally configurable now.
    seen.clear()
    (cfg.home / "config.toml").write_text(
        f'[runner.claude]\nbin = "{runner_dir / "claude"}"\n', encoding="utf-8")
    _stub_binary(runner_dir, "claude")
    fresh2 = config_mod.load(str(cfg.home))
    store.atomic_write(store.spec_path(fresh2.home, "S-1"),
                       "# S-1\n\n## Acceptance criteria\n")

    def _acs_run(argv, **kw):
        seen["path"] = (kw.get("env") or {}).get("PATH")
        class P:
            returncode = 0
            stdout = '{"result": "[]"}'
            stderr = ""
        return P()

    try:
        ops.suggest_acs(fresh2, "S-1", run=_acs_run)
    except Exception:
        pass  # the parse path is not what this asserts
    assert seen.get("path"), "suggest_acs shelled claude with no env"
    assert seen["path"].split(":")[0] == str(runner_dir)
