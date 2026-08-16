"""RB-14: dispatcher-owned test verification. Discovering that a test suite is
red requires no intelligence -- it is a subprocess and an exit code. This moves
that subprocess off the `qa` reconciler (RB-12/T-71's earlier design) and onto
the dispatcher itself: `dispatcher.sync_test_runs` starts/polls `cfg.test_command`
as a tracked, detached subprocess for any `verifying` ticket and routes the
phase on once it exits -- green admits `qa`, red bounces back to `implementing`
with a bounded failure excerpt -- entirely without an agent session.

Every test here drives a real `dispatch()` sweep (`DryRunSessions` -- the
sanctioned mock for the ONE genuinely external boundary, the `claude -p`
spawn) against a real throwaway git repo and a real worktree, and runs a real
`cfg.test_command` subprocess (never mocked) -- per this repo's own QA
convention (CLAUDE.md).
"""
from __future__ import annotations

import os
import sys
import time

from maestro import claims, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import make_origin_and_repo

_PASS_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'
_FAIL_CMD = f'{sys.executable} -c "import sys; sys.stderr.write(\'boom: assertion failed\'); sys.exit(1)"'
_SLEEP_CMD = f'{sys.executable} -c "import time; time.sleep(3)"'


def _write_config(home, repo, *, test_command):
    # Built directly (never via config.toml, which `!r`-embedding a command
    # containing BOTH quote styles can't round-trip through TOML's own
    # quoting rules) -- the same shape the shared `cfg` fixture uses.
    return Config(home=home, repo_path=str(repo), branch_prefix="maestro/",
                 test_command=test_command)


def _seed_verifying(cfg, key, *, acs=("do the thing",)):
    """TicketCreated -> ready -> a REAL worktree -> implementing -> (redirected
    by set_phase) verifying -- the exact path a real board takes once
    `test_command` is configured."""
    home = cfg.home
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: t\npriority: 2\n\n## Intent\nx\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "source": "test", "spec_hash": disp.spec_hash_on_disk(home, key)},
                     actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, key, Phase.QA, reason="pr opened")  # redirected to verifying
    assert snap_mod.load(home, key).phase == Phase.VERIFYING.value
    return home


def _wait_until_dead(pid, *, timeout=10.0):
    """Block until *pid* exits. `dispatch()` here runs IN this test process
    (not as a separate `maestro dispatch` invocation the way it does in
    production), so THIS process is the real parent of the detached test-run
    subprocess it Popen'd -- unlike production, where that process exits
    moments later and init reaps the orphan, nothing reaps it here unless we
    do: an un-waited exited child stays a zombie, and `os.kill(pid, 0)` (what
    `claims.pid_alive` checks) keeps reporting a zombie as alive forever. A
    blocking, targeted `os.waitpid` is both the fix and the correct way to
    observe "it's done" from a real parent."""
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass  # already reaped (e.g. a prior wait on the same pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not claims.pid_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


# ---------------------------------------------------------------------------
# AC1: a real dispatch() sweep starts the run itself and records a
# TestRunCaptured event -- no agent session ever spawned for this ticket.
# ---------------------------------------------------------------------------

def test_dispatch_runs_the_suite_itself_with_no_agent_spawned(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    report1 = disp.dispatch(cfg, sessions, now=1000)
    assert "G-1" not in report1.spawned  # sleeping -- never due for a reconciler
    assert sessions.spawned == []  # nothing spawned yet, for ANY key

    claim = claims.read_claim(home, "G-1")
    assert claim is not None and claim.get("kind") == "testrun"
    _wait_until_dead(claim["pid"])

    # The suite passed, so this SAME fold sweep also re-derives due-ness off
    # the freshly-routed `qa` phase and spawns the next real reconcile step --
    # that's the ordinary next-step machinery, not a session the TEST RUN
    # itself needed. What AC1 asserts is that nothing was ever spawned to
    # RUN/OBSERVE the suite -- confirmed below off the real spawn ledger, not
    # a mock.
    disp.dispatch(cfg, sessions, now=1001)
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-qa")

    events = event_log.read(home, "G-1")
    captured = [e for e in events if e["type"] == "TestRunCaptured"]
    assert len(captured) == 1
    assert captured[0]["payload"]["passed"] is True
    assert claims.read_claim(home, "G-1") is None  # test-run claim released once folded

    # The spawn ledger (agent-spawn cost accounting) only ever charged the ONE
    # real reconciler spawn above -- the test run itself never touched it.
    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    assert ledger.get("G-1", {}).get("recent") == [[1001, 1]]


# ---------------------------------------------------------------------------
# AC2: a red result routes back to `implementing`, spawns no `qa` session, and
# the routing event carries a bounded excerpt of the real failure output.
# ---------------------------------------------------------------------------

def test_red_result_routes_to_implementing_with_failure_excerpt_and_no_qa_spawn(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_FAIL_CMD)
    _seed_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    # The SAME sweep that folds the result also re-derives due-ness off the
    # freshly-routed phase -- IMPLEMENTING is active, so this call both routes
    # AND (correctly) spawns the next implementing reconciler in one pass.
    report2 = disp.dispatch(cfg, sessions, now=1001)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert "G-1" in report2.spawned
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-implementing")

    events = event_log.read(home, "G-1")
    captured = [e for e in events if e["type"] == "TestRunCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "boom: assertion failed" in captured["payload"]["failure_excerpt"]

    routed = [e for e in events if e["type"] == "PhaseChanged" and e["payload"]["phase"] == "implementing"][-1]
    assert "boom: assertion failed" in routed["payload"]["reason"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)  # never a qa session


# ---------------------------------------------------------------------------
# AC3: a green result is what admits `qa` -- a real sweep then spawns `qa`
# exactly once.
# ---------------------------------------------------------------------------

def test_green_result_admits_exactly_one_qa_spawn(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    # The SAME sweep that folds the result also re-derives due-ness off the
    # freshly-routed phase -- QA is active, so this call both routes AND
    # (correctly) spawns the qa reconciler in one pass.
    report2 = disp.dispatch(cfg, sessions, now=1001)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    assert report2.spawned.count("G-1") == 1
    assert sessions.spawned[-1][1].startswith("/maestro-reconcile-qa")

    # A later sweep does NOT spawn it again -- the claim from this spawn holds
    # the key (a real backend would; DryRunSessions' own `_active` already
    # reflects that spawn, so a further sweep sees it as claimed, not due).
    report3 = disp.dispatch(cfg, sessions, now=1002)
    assert "G-1" not in report3.spawned


# ---------------------------------------------------------------------------
# AC4: dispatch() never blocks on the run -- a sweep with a slow command in
# flight completes in its normal (fast) time, and the result is folded later.
# ---------------------------------------------------------------------------

def test_dispatch_does_not_block_on_a_slow_test_run(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_SLEEP_CMD)
    _seed_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    started = time.monotonic()
    disp.dispatch(cfg, sessions, now=1000)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"dispatch() blocked for {elapsed:.2f}s on a 5s test command"

    # Still verifying, still in flight -- nothing folded yet.
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value
    claim = claims.read_claim(home, "G-1")
    assert claim is not None and claims.pid_alive(claim["pid"])

    # A sweep while it's still running is ALSO fast, and does not restart it.
    started2 = time.monotonic()
    disp.dispatch(cfg, sessions, now=1001)
    assert time.monotonic() - started2 < 2.0
    assert claims.read_claim(home, "G-1")["pid"] == claim["pid"]  # same run, not restarted

    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, sessions, now=1002)  # a LATER sweep folds it
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC5: the in-flight run holds a claim through the existing claims machinery
# -- no second liveness/dedup mechanism.
# ---------------------------------------------------------------------------

def test_in_flight_run_holds_the_ordinary_per_key_claim(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_SLEEP_CMD)
    _seed_verifying(cfg, "G-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    rows = claims.describe_claims(home)
    row = next(r for r in rows if r["key"] == "G-1")
    assert row["claimed"] is True
    assert row["verdict"] == "confirmed"
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])


# ---------------------------------------------------------------------------
# AC6: the agent path becomes unnecessary -- a reconciler that never calls
# `capture-tests` still gets verified purely by the dispatcher.
# ---------------------------------------------------------------------------

def test_ticket_gets_verified_without_the_agent_ever_calling_capture_tests(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_verifying(cfg, "G-1")  # never called ops.capture_tests itself

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, DryRunSessions(), now=1001)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC7: a test run is never charged as an agent spawn -- the spawn ledger and
# spend/weight machinery are never touched by it.
# ---------------------------------------------------------------------------

def test_test_run_claim_does_not_eat_a_max_concurrency_slot(tmp_path, home):
    """A REAL backend's `list_active()` delegates to `claims.active_keys`,
    which has no notion of `kind` -- it reports a `verifying` ticket's
    in-flight test-run claim as "active" exactly like an agent session's.
    `dispatch()`'s own fleet-wide `max_concurrency` slot budget must not be
    eaten by it regardless: a `testrun` claim is emulated here via
    `DryRunSessions(active={...})` (this mock's own `active` param stands in
    for whatever a real backend's `list_active()` would report), and a
    SEPARATE, genuinely due ticket must still get its slot even at
    `max_concurrency=1`."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    cfg.max_concurrency = 1
    _seed_verifying(cfg, "G-1")
    # A genuinely alive pid (this test process's own) -- sync_test_runs's own
    # hook, which also runs inside the one dispatch() call below, must see it
    # as still in flight and leave it alone, not fold it away before the slot
    # computation this test is actually exercising even runs.
    claims.write_claim(home, "G-1", os.getpid(), "testrun-G-1", kind="testrun")

    key = "G-2"
    store.atomic_write(store.spec_path(home, key), f"# {key}: t\npriority: 2\n\n## Intent\nx\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "source": "test", "spec_hash": disp.spec_hash_on_disk(home, key)},
                     actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")

    sessions = DryRunSessions(active={"G-1"})  # simulates a real backend's list_active()
    report = disp.dispatch(cfg, sessions, now=1000)

    assert "G-2" in report.spawned


def test_test_run_never_touches_the_spawn_ledger(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_verifying(cfg, "G-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    ledger_path = disp._spawn_ledger_path(home)
    ledger = store.read_json(ledger_path, {}) or {}
    assert "G-1" not in ledger  # no agent-spawn weight was ever recorded for it

    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])


def test_test_run_concurrency_is_bounded_by_an_explicit_cap(tmp_path, home, monkeypatch):
    monkeypatch.setattr(disp, "TEST_RUN_CONCURRENCY", 1)
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_SLEEP_CMD)
    _seed_verifying(cfg, "G-1")
    _seed_verifying(cfg, "G-2")

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    in_flight = [k for k in ("G-1", "G-2")
                if (c := claims.read_claim(home, k)) is not None and c.get("kind") == "testrun"]
    assert len(in_flight) == 1, "only TEST_RUN_CONCURRENCY run(s) may be in flight at once"

    for k in ("G-1", "G-2"):
        c = claims.read_claim(home, k)
        if c is not None:
            _wait_until_dead(c["pid"])


# ---------------------------------------------------------------------------
# AC8: `mode: local` never wedges even if it somehow reached `verifying`.
# ---------------------------------------------------------------------------

def test_mode_local_ticket_in_verifying_never_wedges(tmp_path, home):
    target = tmp_path / "vault"
    target.mkdir()
    cfg = Config(home=home, test_command=_FAIL_CMD,
                repos={"vault": {"path": str(target), "mode": "local", "default": True}})
    key = "V-1"
    store.atomic_write(store.spec_path(home, key), f"# {key}\n\n## Intent\nx\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "source": "test", "spec_hash": disp.spec_hash_on_disk(home, key)},
                     actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="local target ready")
    # A real `mode: local` ticket never redirects to verifying (see
    # test_test_run_gate.py) -- forcibly land it there to prove the defensive
    # branch in `sync_test_runs` itself never wedges.
    event_log.append(home, key, "PhaseChanged", {"phase": "verifying"}, actor="test")
    snap_mod.rebuild(home, key)

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert snap_mod.load(home, key).phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC9: reproduces the measured case -- a ticket whose only defect is a stale
# generated doc bounces back to `implementing` without any `qa` session ever
# being spawned.
# ---------------------------------------------------------------------------

def test_reproduces_the_stale_generated_doc_incident_end_to_end(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "generated.md").write_text("STALE\n", encoding="utf-8")
    checker = repo / "check_doc.py"
    checker.write_text(
        "import pathlib, sys\n"
        "doc = pathlib.Path('docs/generated.md')\n"
        "sys.exit(0 if doc.read_text() == 'GENERATED\\n' else 1)\n",
        encoding="utf-8")
    from conftest import git
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add stale-doc checker", cwd=repo)
    cfg = _write_config(home, repo, test_command=f"{sys.executable} check_doc.py")
    _seed_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, sessions, now=1001)

    assert snap_mod.load(home, "G-1").phase == Phase.IMPLEMENTING.value
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)
