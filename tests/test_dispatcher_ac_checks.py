"""T-79: opt-in, machine-checkable AC annotations, enforced at the T-74
`verifying` stage. Once the whole suite (`TestRunCaptured`) is green,
`dispatcher._route_test_run` also runs every ANNOTATED AC's own `test:`/
`check:` check at the same tree state (`ops.run_ac_checks`) before admitting
`qa` -- a real subprocess, never an agent session.

Every test here drives a real `dispatch()` sweep (`DryRunSessions`) against a
real throwaway git repo and worktree, per this repo's own QA convention.
"""
from __future__ import annotations

import sys
import time

from maestro import claims, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo

_PYTEST = f"{sys.executable} -m pytest -q"
_PASS_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'


def _write_config(home, repo, *, test_command):
    return Config(home=home, repo_path=str(repo), branch_prefix="maestro/",
                 test_command=test_command)


def _seed_to_worktree(cfg, key, *, acs):
    """TicketCreated -> ready -> a real worktree, stopping BEFORE implementing
    so the caller can commit onto the ticket branch (the diff a `test:`
    annotation checks presence against) before the phase advances."""
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
    return store.worktree_path(home, key)


def _advance_to_verifying(cfg, key):
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, key, Phase.QA, reason="pr opened")  # redirected to verifying
    assert snap_mod.load(cfg.home, key).phase == Phase.VERIFYING.value


def _wait_until_dead(pid, *, timeout=15.0):
    import os
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not claims.pid_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


def _run_verifying_to_completion(cfg, key, sessions):
    """Start the async suite run, wait for it to exit, then a second sweep
    folds it and (synchronously) runs any annotated AC checks."""
    disp.dispatch(cfg, sessions, now=1000)
    claim = claims.read_claim(cfg.home, key)
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, sessions, now=1001)


# ---------------------------------------------------------------------------
# `check:` annotation
# ---------------------------------------------------------------------------

def test_green_check_annotation_admits_qa_with_no_agent_spawned_for_it(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    wt = _seed_to_worktree(cfg, "G-1", acs=["flag exists (check: test -f flag.txt)"])
    (wt / "flag.txt").write_text("ok\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    events = event_log.read(home, "G-1")
    captured = [e for e in events if e["type"] == "AcCheckCaptured"]
    assert len(captured) == 1
    assert captured[0]["payload"]["passed"] is True
    assert captured[0]["payload"]["kind"] == "check"
    # No agent session was ever spawned to run this check -- only the eventual
    # `qa` reconciler shows up on the spawn ledger.
    ledger = store.read_json(disp._spawn_ledger_path(home), {}) or {}
    assert ledger.get("G-1", {}).get("recent") == [[1001, 1]]


def test_red_check_annotation_routes_back_to_implementing_no_qa_spawn(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    wt = _seed_to_worktree(cfg, "G-1", acs=["flag exists (check: test -f flag.txt)"])
    # deliberately never create flag.txt
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    events = event_log.read(home, "G-1")
    captured = [e for e in events if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    routed = [e for e in events if e["type"] == "PhaseChanged"
             and e["payload"]["phase"] == "implementing"][-1]
    assert "AC#1" in routed["payload"]["reason"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


def test_check_annotation_result_is_cached_per_tree_state(tmp_path, home):
    """A second call at the SAME tree state reuses the captured record rather
    than re-running the check (the `ops.capture_tests` caching rule, applied
    per-AC) -- proven directly against `ops.run_ac_checks`, since a repeat
    `set_phase(..., qa)` at an UNCHANGED tree short-circuits the verifying
    redirect entirely (`_tests_stale_reason` already sees a passing capture)
    and so never re-enters `verifying` to exercise this at the dispatcher
    layer a second time."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    wt = _seed_to_worktree(cfg, "G-1", acs=["flag exists (check: test -f flag.txt)"])
    (wt / "flag.txt").write_text("ok\n")
    _advance_to_verifying(cfg, "G-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value

    result = ops.run_ac_checks(cfg, "G-1", wt)  # same tree state, called again
    assert result["all_passed"] is True

    events = event_log.read(home, "G-1")
    captured = [e for e in events if e["type"] == "AcCheckCaptured"]
    assert len(captured) == 1, "identical tree state must not re-run the check"


# ---------------------------------------------------------------------------
# `test:` annotation -- diff-presence semantics (the T-55 false-attestation
# class this exists to close).
# ---------------------------------------------------------------------------

def _commit_test_file(repo_or_wt, rel_path, body, *, branch=None):
    if branch:
        git("checkout", "-q", "-b", branch, cwd=repo_or_wt)
    p = repo_or_wt / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    git("add", "-A", cwd=repo_or_wt)
    git("commit", "-q", "-m", "add test", cwd=repo_or_wt)


def test_named_test_added_by_the_diff_and_passing_admits_qa(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=["widget works (test: tests/test_widget.py::test_widget)"])
    _commit_test_file(wt, "tests/test_widget.py",
                      "def test_widget():\n    assert True\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is True
    assert captured["payload"]["kind"] == "test"


def test_named_test_not_added_by_diff_fails_even_with_a_green_suite(tmp_path, home):
    """Reproduces the T-55 seq-130 class: the suite is green (nothing broke),
    but the AC's named test was never actually added by THIS diff -- a stale
    or pre-existing test of the same name must not satisfy the gate."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=["widget works (test: tests/test_widget.py::test_widget)"])
    # The branch adds a DIFFERENT test in that file, not the named one.
    _commit_test_file(wt, "tests/test_widget.py",
                      "def test_something_else():\n    assert True\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is False
    assert "not found among tests added by this diff" in captured["payload"]["failure_excerpt"]
    assert all("qa" not in p for _k, p, *_r in sessions.spawned)


def test_bare_file_path_form_satisfied_by_some_added_test_in_that_file(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    wt = _seed_to_worktree(cfg, "G-1", acs=["widget works (test: tests/test_widget.py)"])
    _commit_test_file(wt, "tests/test_widget.py",
                      "def test_anything():\n    assert True\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


def test_bare_file_path_form_fails_when_diff_adds_no_test_in_that_file(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    # A baseline test on `main` so the suite still collects/passes something
    # once the branch's own file adds no test -- otherwise pytest's own
    # "no tests collected" exit code would fail the SUITE, not this AC check,
    # muddying what this test is actually pinning.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_baseline.py").write_text("def test_baseline():\n    assert True\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "baseline test", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    wt = _seed_to_worktree(cfg, "G-1", acs=["widget works (test: tests/test_widget.py)"])
    # The file exists (committed), but adds no NEW test function -- e.g. only
    # a comment changed.
    _commit_test_file(wt, "tests/test_widget.py", "# placeholder, no test yet\n")
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    snap = snap_mod.load(home, "G-1")
    assert snap.phase == Phase.IMPLEMENTING.value
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert "no test added by this diff" in captured["payload"]["failure_excerpt"]


# ---------------------------------------------------------------------------
# The `awaiting-ci` gate: an annotated AC requires a current-tree passing
# captured check instead of `verify-ac`; unannotated ACs are unaffected.
# ---------------------------------------------------------------------------

def test_awaiting_ci_gate_requires_captured_check_not_attestation_for_annotated_ac(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    wt = _seed_to_worktree(
        cfg, "G-1",
        acs=["widget works (test: tests/test_widget.py::test_widget)", "docs updated"])
    _commit_test_file(wt, "tests/test_widget.py", "def test_widget():\n    assert True\n")
    _advance_to_verifying(cfg, "G-1")

    disp.dispatch(cfg, DryRunSessions(), now=1000)
    claim = claims.read_claim(home, "G-1")
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value

    # Bounce back to implementing to attempt awaiting-ci from there.
    ops.set_phase(cfg, "G-1", Phase.IMPLEMENTING, reason="test", force=True)

    # AC#2 (unannotated) still needs verify-ac even though AC#1's check
    # already passed; AC#1 needs NO verify-ac call at all.
    from maestro import store as store_mod
    with __import__("pytest").raises(store_mod.MaestroError, match="unverified"):
        ops.set_phase(cfg, "G-1", Phase.AWAITING_CI, reason="pr opened")

    ops.verify_ac(cfg, "G-1", 2, {"what": "read docs", "where": "README", "result": "ok"})
    ops.set_phase(cfg, "G-1", Phase.AWAITING_CI, reason="pr opened")  # now clean
    assert snap_mod.load(home, "G-1").phase == Phase.AWAITING_CI.value


def test_awaiting_ci_gate_still_blocks_when_annotated_check_never_ran(tmp_path, home):
    """An annotated AC with NO captured check yet (never reached verifying)
    must still block -- a bare verify-ac attestation does not substitute."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PYTEST)
    _seed_to_worktree(cfg, "G-1", acs=["widget works (test: tests/test_widget.py::test_widget)"])
    ops.set_phase(cfg, "G-1", Phase.IMPLEMENTING, reason="worktree ready")

    ops.verify_ac(cfg, "G-1", 1, {"what": "eyeballed it", "where": "n/a", "result": "looks fine"})
    from maestro import store as store_mod
    with __import__("pytest").raises(store_mod.MaestroError, match="unverified"):
        ops.set_phase(cfg, "G-1", Phase.AWAITING_CI, reason="pr opened")


# ---------------------------------------------------------------------------
# Ships dark: `test_command` unset, and separately `mode: local`, make
# annotated ACs behave byte-identically to plain prose ACs.
# ---------------------------------------------------------------------------

def test_ships_dark_when_test_command_unset(home):
    cfg = Config(home=home)  # no test_command at all
    key = "G-1"
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: t\npriority: 2\n\n## Intent\nx\n\n"
                       "## Acceptance criteria\n"
                       "- [ ] widget works (test: tests/test_widget.py::test_widget)\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="t")

    from maestro import store as store_mod
    with __import__("pytest").raises(store_mod.MaestroError, match="unverified"):
        ops.set_phase(cfg, key, Phase.AWAITING_CI, reason="pr opened")

    # verify-ac (plain self-attestation) is what satisfies it -- the
    # annotation is completely inert with test_command unset.
    ops.verify_ac(cfg, key, 1, {"what": "ran it", "where": "n/a", "result": "ok"})
    ops.set_phase(cfg, key, Phase.AWAITING_CI, reason="pr opened")
    assert snap_mod.load(home, key).phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC8: no bulk rewrite -- a board with only unannotated ACs dispatches
# identically to the pre-T-79 RB-14 behavior (no AcCheckCaptured events ever
# appended, same phase routing).
# ---------------------------------------------------------------------------

def test_unannotated_spec_dispatches_identically_to_before_this_ticket(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_to_worktree(cfg, "G-1", acs=["do the thing", "and another thing"])
    _advance_to_verifying(cfg, "G-1")

    sessions = DryRunSessions()
    _run_verifying_to_completion(cfg, "G-1", sessions)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    events = event_log.read(home, "G-1")
    assert [e for e in events if e["type"] == "AcCheckCaptured"] == []
    assert len([e for e in events if e["type"] == "TestRunCaptured"]) == 1


def test_ships_dark_for_a_mode_local_ticket(tmp_path, home):
    target = tmp_path / "vault"
    target.mkdir()
    cfg = Config(home=home, test_command=_PYTEST,
                repos={"vault": {"path": str(target), "mode": "local", "default": True}})
    key = "V-1"
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Intent\nx\n\n## Acceptance criteria\n"
                       "- [ ] note written (check: true)\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="t")

    from maestro import store as store_mod
    with __import__("pytest").raises(store_mod.MaestroError, match="unverified"):
        ops.set_phase(cfg, key, Phase.AWAITING_CI, reason="done")

    ops.verify_ac(cfg, key, 1, {"what": "wrote it", "where": "note.md", "result": "ok"})
    ops.set_phase(cfg, key, Phase.AWAITING_CI, reason="done")
    assert snap_mod.load(home, key).phase == Phase.AWAITING_CI.value
