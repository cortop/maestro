"""T-83: `test_command` is a per-repo override falling back to the board-wide
`[maestro]` value, carried on `RepoBinding`, with every RB-12/RB-14/T-79 gate
routed through `repos.resolve(...).test_command` instead of reading
`cfg.test_command` directly -- so a home spanning a Go/Bazel repo and a
TS/yarn repo can each declare a correct suite command, instead of the whole
verification tier staying dark (the only safe choice today).

Every test here drives the real `maestro`/`ops`/`dispatcher` surface -- a real
`config.load()` over a real config.toml, a real `dispatch()` sweep against a
real throwaway git repo per repo, and a real subprocess suite run -- per this
repo's own QA convention (CLAUDE.md).
"""
from __future__ import annotations

import sys
import time

from maestro import claims, config as config_mod, dispatcher as disp, event_log, ops, \
    repos as repos_mod, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo

_CMD_A = f'{sys.executable} -c "import sys; sys.exit(0)"'
_CMD_B = f'{sys.executable} -c "import sys; sys.exit(0 if True else 1)"'
_BOARD_CMD = f'{sys.executable} -c "import sys; sys.exit(int(False))"'
_PYTEST = f"{sys.executable} -m pytest -q"
# A board-wide default that would fail loudly (non-pytest args) if `_run_named_test`
# ever fell back to it instead of the bound repo's own `test:` runner -- proves
# AC3 for the `test:` annotation path specifically.
_BOGUS_BOARD_CMD = f"{sys.executable} -c \"import sys; sys.exit(1)\""


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


def _seed_impl(cfg, key, repo_name, *, acs=("do the thing",)):
    """TicketCreated -> ready -> a REAL worktree -> implementing, bound to
    *repo_name* via spec frontmatter -- stops just short of the `qa` call so
    callers can assert whether it redirects to `verifying` or not."""
    home = cfg.home
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: t\npriority: 2\nrepo: {repo_name}\n\n## Intent\nx\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "source": "test", "repo": repo_name,
                      "spec_hash": disp.spec_hash_on_disk(home, key)},
                     actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")


def _seed_verifying(cfg, key, repo_name, *, acs=("do the thing",)):
    _seed_impl(cfg, key, repo_name, acs=acs)
    ops.set_phase(cfg, key, Phase.QA, reason="pr opened")  # redirected to verifying
    assert snap_mod.load(cfg.home, key).phase == Phase.VERIFYING.value


def _two_repo_config(home, repo_a, repo_b, *, board_test_command=None,
                     cmd_a=_CMD_A, cmd_b=_CMD_B):
    return Config(home=home, test_command=board_test_command, repos={
        "goapp": {"path": str(repo_a), "base_branch": "main", "branch_prefix": "maestro/",
                 "test_command": cmd_a},
        "web": {"path": str(repo_b), "base_branch": "main", "branch_prefix": "maestro/",
               "test_command": cmd_b},
    })


# ---------------------------------------------------------------------------
# AC1: [repos.<name>] accepts test_command; loads without raising, and each
# binding exposes its own resolved command.
# ---------------------------------------------------------------------------

def test_repo_table_accepts_test_command_and_binding_exposes_it(home):
    (home / "config.toml").write_text(
        '[maestro]\ntest_command = "pytest -q"\n\n'
        '[repos.goapp]\npath = "/repo/goapp"\ntest_command = "go test ./..."\n\n'
        '[repos.web]\npath = "/repo/web"\ntest_command = "yarn test"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))  # must not raise
    assert cfg.test_command == "pytest -q"

    store.atomic_write(store.spec_path(home, "G-1"), "# G-1\nrepo: goapp\n\n## Intent\nx\n")
    store.atomic_write(store.spec_path(home, "W-1"), "# W-1\nrepo: web\n\n## Intent\nx\n")
    assert repos_mod.resolve(cfg, home, "G-1").test_command == "go test ./..."
    assert repos_mod.resolve(cfg, home, "W-1").test_command == "yarn test"


# ---------------------------------------------------------------------------
# AC2: precedence both directions -- an unset per-repo value inherits the
# board-wide default, and a set per-repo value wins over it.
# ---------------------------------------------------------------------------

def test_resolve_precedence_both_directions_across_two_keys(home):
    (home / "config.toml").write_text(
        '[maestro]\ntest_command = "pytest -q"\n\n'
        '[repos.goapp]\npath = "/repo/goapp"\ntest_command = "go test ./..."\n\n'
        '[repos.web]\npath = "/repo/web"\n',  # no override -- inherits board-wide
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "G-1"), "# G-1\nrepo: goapp\n\n## Intent\nx\n")
    store.atomic_write(store.spec_path(home, "W-1"), "# W-1\nrepo: web\n\n## Intent\nx\n")

    goapp_binding = repos_mod.resolve(cfg, home, "G-1")
    web_binding = repos_mod.resolve(cfg, home, "W-1")
    assert goapp_binding.test_command == "go test ./..."  # per-repo override wins
    assert web_binding.test_command == "pytest -q"         # unset inherits board-wide default


# ---------------------------------------------------------------------------
# AC3: the named gates each resolve through the per-key binding, never
# `cfg.test_command` directly -- proven by giving the bound repo a DIFFERENT
# command than the board-wide default and observing the repo's own command
# is what actually governs each gate's behavior.
# ---------------------------------------------------------------------------

def test_annotations_active_and_stale_reason_resolve_per_key_not_board_wide(home):
    cfg = Config(home=home, test_command=None, repos={
        # Board-wide is unset; goapp's own override arms the gate for IT alone.
        "goapp": {"path": "/repo/goapp", "test_command": "go test ./..."},
        "web": {"path": "/repo/web"},  # no override, board-wide also unset
    })
    store.atomic_write(store.spec_path(home, "G-1"), "# G-1\nrepo: goapp\n\n## Intent\nx\n")
    store.atomic_write(store.spec_path(home, "W-1"), "# W-1\nrepo: web\n\n## Intent\nx\n")

    assert ops._annotations_active(cfg, "G-1") is True
    assert ops._annotations_active(cfg, "W-1") is False

    snap_g = snap_mod.Snapshot(key="G-1", phase="implementing")
    snap_w = snap_mod.Snapshot(key="W-1", phase="implementing")
    # goapp resolves a command -> the gate is live (a non-None reason, since
    # nothing has been captured yet); web resolves none -> satisfied (None).
    assert ops._tests_stale_reason(cfg, "G-1", snap_g) is not None
    assert ops._tests_stale_reason(cfg, "W-1", snap_w) is None


def test_capture_tests_uses_the_bound_repos_own_command(tmp_path, home):
    _origin_a, repo_a = make_origin_and_repo(tmp_path, name="goapp")
    _origin_b, repo_b = make_origin_and_repo(tmp_path, name="web")
    cfg = _two_repo_config(home, repo_a, repo_b, board_test_command=None)
    _seed_impl(cfg, "G-1", "goapp")
    _seed_impl(cfg, "W-1", "web")

    g = ops.capture_tests(cfg, "G-1")
    w = ops.capture_tests(cfg, "W-1")
    assert g["command"] == _CMD_A
    assert w["command"] == _CMD_B
    assert g["command"] != w["command"]

    g_events = [e for e in event_log.read(home, "G-1") if e["type"] == "TestRunCaptured"]
    w_events = [e for e in event_log.read(home, "W-1") if e["type"] == "TestRunCaptured"]
    assert g_events[0]["payload"]["command"] == _CMD_A
    assert w_events[0]["payload"]["command"] == _CMD_B


def test_run_named_test_uses_the_bound_repos_own_command(tmp_path, home):
    """T-79's `test:` annotation (`ops._run_named_test`, via `run_ac_checks`)
    must build its command off the BOUND repo's own `test_command` -- proven
    by giving the board-wide default a command that would fail if it were
    used instead of the repo's real pytest runner."""
    _origin, repo = make_origin_and_repo(tmp_path, name="goapp")
    cfg = Config(home=home, test_command=_BOGUS_BOARD_CMD, repos={
        "goapp": {"path": str(repo), "base_branch": "main", "branch_prefix": "maestro/",
                 "test_command": _PYTEST},
    })
    wt = store.worktree_path(home, "G-1")
    ac_lines = "- [ ] widget works (test: tests/test_widget.py::test_widget)"
    store.atomic_write(store.spec_path(home, "G-1"),
                       f"# G-1: t\npriority: 2\nrepo: goapp\n\n## Intent\nx\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, "G-1", "TicketCreated",
                     {"title": "G-1", "source": "test", "repo": "goapp"}, actor="d")
    snap_mod.rebuild(home, "G-1")
    ops.set_phase(cfg, "G-1", Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, "G-1")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_widget.py").write_text("def test_widget():\n    assert True\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "add test", cwd=wt)

    result = ops.run_ac_checks(cfg, "G-1", wt)
    assert result["all_passed"] is True
    captured = [e for e in event_log.read(home, "G-1") if e["type"] == "AcCheckCaptured"][0]
    assert captured["payload"]["passed"] is True
    assert captured["payload"]["command"].startswith(_PYTEST)


# ---------------------------------------------------------------------------
# AC4: a real dispatch() sweep over two repos, one `verifying` ticket each,
# runs EACH ticket's own command -- the two TestRunCaptured events record the
# two different command strings.
# ---------------------------------------------------------------------------

def test_dispatch_sweep_runs_each_repos_own_test_command(tmp_path, home):
    _origin_a, repo_a = make_origin_and_repo(tmp_path, name="goapp")
    _origin_b, repo_b = make_origin_and_repo(tmp_path, name="web")
    cfg = _two_repo_config(home, repo_a, repo_b)
    _seed_verifying(cfg, "G-1", "goapp")
    _seed_verifying(cfg, "W-1", "web")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    for key in ("G-1", "W-1"):
        claim = claims.read_claim(home, key)
        assert claim is not None and claim.get("kind") == "testrun"
        _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, sessions, now=1001)

    g_events = [e for e in event_log.read(home, "G-1") if e["type"] == "TestRunCaptured"]
    w_events = [e for e in event_log.read(home, "W-1") if e["type"] == "TestRunCaptured"]
    assert len(g_events) == 1 and g_events[0]["payload"]["command"] == _CMD_A
    assert len(w_events) == 1 and w_events[0]["payload"]["command"] == _CMD_B
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    assert snap_mod.load(home, "W-1").phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC5: `sync_test_runs` no longer short-circuits the whole sweep on a
# board-wide unset -- a goapp ticket (own repo has a test_command) still gets
# its run started, while a ticket on a command-less repo is left alone (it
# never even redirects into `verifying` -- `_tests_stale_reason` is already
# satisfied for it).
# ---------------------------------------------------------------------------

def test_sync_test_runs_arms_per_key_when_board_wide_unset_but_repo_set(tmp_path, home):
    _origin_a, repo_a = make_origin_and_repo(tmp_path, name="goapp")
    _origin_b, repo_b = make_origin_and_repo(tmp_path, name="web")
    cfg = Config(home=home, test_command=None, repos={
        "goapp": {"path": str(repo_a), "base_branch": "main", "branch_prefix": "maestro/",
                 "test_command": _CMD_A},
        "web": {"path": str(repo_b), "base_branch": "main", "branch_prefix": "maestro/"},
                 # no test_command, and board-wide is also unset
    })
    _seed_impl(cfg, "G-1", "goapp")
    _seed_impl(cfg, "W-1", "web")
    ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")  # redirected -- goapp has a command
    ops.set_phase(cfg, "W-1", Phase.QA, reason="pr opened")  # NOT redirected -- no command anywhere
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value
    assert snap_mod.load(home, "W-1").phase == Phase.QA.value

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    claim = claims.read_claim(home, "G-1")
    assert claim is not None and claim.get("kind") == "testrun"
    _wait_until_dead(claim["pid"])
    disp.dispatch(cfg, sessions, now=1001)

    assert snap_mod.load(home, "G-1").phase == Phase.QA.value
    g_events = [e for e in event_log.read(home, "G-1") if e["type"] == "TestRunCaptured"]
    assert len(g_events) == 1 and g_events[0]["payload"]["command"] == _CMD_A

    # The command-less-repo ticket was left alone: no test-run claim was ever
    # taken for it, and no TestRunCaptured event was ever appended.
    assert claims.read_claim(home, "W-1") is None
    assert not [e for e in event_log.read(home, "W-1") if e["type"] == "TestRunCaptured"]


# ---------------------------------------------------------------------------
# AC6: the T-79 annotation regime arms per key -- `_annotations_active` is
# True for a goapp key and False for a key on a command-less repo, and the
# `awaiting-ci` gate on an annotated AC follows suit.
# ---------------------------------------------------------------------------

def test_awaiting_ci_gate_on_annotated_ac_follows_per_key_arming(home):
    cfg = Config(home=home, test_command=None, repos={
        "goapp": {"path": "/repo/goapp", "test_command": "go test ./..."},
        "web": {"path": "/repo/web"},  # no test_command, board-wide also unset
    })
    # T-85 landed after this test and added its own, independent unconditional
    # awaiting-ci gate (every current-hash AC needs a passing spec-axis QA
    # verdict, regardless of T-79 annotation arming) -- orthogonal to what this
    # test is about (the T-79 per-key arming of _acs_unverified_count), so opt
    # out of it here exactly like every other pre-T-85 test in this suite does.
    cfg.awaiting_ci_qa_gate = False
    for key, repo_name in (("G-1", "goapp"), ("W-1", "web")):
        store.atomic_write(
            store.spec_path(home, key),
            f"# {key}: t\npriority: 2\nrepo: {repo_name}\n\n## Intent\nx\n\n"
            "## Acceptance criteria\n"
            "- [ ] widget works (check: test -f flag.txt)\n")
        event_log.append(home, key, "TicketCreated", {"title": key, "source": "test",
                                                       "repo": repo_name}, actor="d")
        snap_mod.rebuild(home, key)
        ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="t")

    assert ops._annotations_active(cfg, "G-1") is True
    assert ops._annotations_active(cfg, "W-1") is False

    # G-1 (armed): a bare verify-ac does NOT satisfy the annotated AC -- only
    # a captured check would (never taken here), so it stays blocked.
    ops.verify_ac(cfg, "G-1", 1, {"what": "eyeballed it", "where": "n/a", "result": "looks fine"})
    with __import__("pytest").raises(store.MaestroError, match="unverified"):
        ops.set_phase(cfg, "G-1", Phase.AWAITING_CI, reason="pr opened")

    # W-1 (not armed): the SAME annotation is inert -- a bare verify-ac
    # satisfies it, byte-identical to an unannotated AC.
    ops.verify_ac(cfg, "W-1", 1, {"what": "eyeballed it", "where": "n/a", "result": "looks fine"})
    ops.set_phase(cfg, "W-1", Phase.AWAITING_CI, reason="pr opened")
    assert snap_mod.load(home, "W-1").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC7: ships dark unchanged -- a home with only a board-wide test_command, and
# a home with none at all, both behave exactly as before this ticket over a
# full dispatch sweep (proven above all by the untouched, still-green
# tests/test_dispatcher_test_runs.py and tests/test_dispatcher_ac_checks.py --
# this test adds the direct "still just one config knob" cross-check).
# ---------------------------------------------------------------------------

def test_ships_dark_board_wide_only_and_unset_both_unaffected_by_repo_table(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)

    # Board-wide only, no [repos.*] table at all -- the implicit default binding
    # must carry cfg.test_command through untouched.
    cfg = Config(home=home, repo_path=str(repo), branch_prefix="maestro/",
                test_command=_BOARD_CMD)
    binding = repos_mod.implicit_default(cfg)
    assert binding.test_command == _BOARD_CMD

    # Neither board-wide nor any repo set -- fully dark.
    cfg_dark = Config(home=home, repo_path=str(repo), branch_prefix="maestro/")
    assert repos_mod.implicit_default(cfg_dark).test_command is None
    assert disp.sync_test_runs(cfg_dark, now=1000) == {"checked": 0}


# ---------------------------------------------------------------------------
# AC8: the unknown-key fail-closed rule still holds for a typo'd key, and it
# also catches a typo of `test_command` itself; the generated template
# documents the per-repo override.
# ---------------------------------------------------------------------------

def test_unknown_repo_key_still_fails_closed_including_a_test_command_typo(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/x"\n\n'
        '[repos.goapp]\npath = "/repo/goapp"\ntest_commnd = "go test ./..."\n',
        encoding="utf-8")
    try:
        config_mod.load(str(home))
        raise AssertionError("expected MaestroError")
    except store.MaestroError as e:
        assert "goapp" in str(e)
        assert "test_commnd" in str(e)


def test_default_config_template_documents_per_repo_test_command():
    assert "test_command" in config_mod.DEFAULT_CONFIG_TOML
    # Both the board-wide knob and its per-repo override are documented.
    idx_board = config_mod.DEFAULT_CONFIG_TOML.index("test_command = \".venv")
    idx_repo = config_mod.DEFAULT_CONFIG_TOML.index("test_command = \"go test")
    assert idx_board >= 0 and idx_repo >= 0
