"""T-287: every git subprocess call the dispatcher makes in a sweep is bounded, and a
timed-out fetch fails only its own repo for that sweep."""
import ast
import subprocess
from pathlib import Path

from maestro import dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import add_worktree, make_origin_and_repo, seed_phase


def _is_git_argv(node: ast.AST) -> bool:
    return (isinstance(node, ast.List) and node.elts
            and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "git")


def test_every_git_subprocess_call_has_a_timeout():
    src = Path(disp.__file__).read_text()
    unbounded = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_run = (isinstance(f, ast.Attribute) and f.attr in ("run", "Popen", "check_output", "call")
                  and isinstance(f.value, ast.Name) and f.value.id == "subprocess")
        if not is_run or not node.args or not _is_git_argv(node.args[0]):
            continue
        if not any(kw.arg == "timeout" for kw in node.keywords):
            unbounded.append(node.lineno)
        # `communicate()` on a git Popen must also carry a timeout
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "communicate"
                and not any(kw.arg == "timeout" for kw in node.keywords) and not node.args):
            unbounded.append(node.lineno)
    # multi-line `subprocess.run(\n ["git", ...` calls are caught above via args[0]
    assert unbounded == [], f"git calls without timeout at dispatcher.py lines {unbounded}"


def _setup(home, cfg, tmp_path):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    seed_phase(home, "T-wt", Phase.AWAITING_CI)
    add_worktree(repo, home, "T-wt", "maestro/T-wt")
    return repo


def test_fetch_uses_repo_worktree_fetch_timeout(home, cfg, tmp_path, monkeypatch):
    _setup(home, cfg, tmp_path)
    cfg.worktree_fetch_timeout = 77
    real_run = subprocess.run
    seen = []

    def spy(argv, *a, **k):
        if "fetch" in argv:
            seen.append(k.get("timeout"))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(disp.subprocess, "run", spy)
    disp.sync_worktrees(cfg)
    assert seen and set(seen) == {77}


def test_fetch_timeout_fails_only_its_repo_and_sweep_still_spawns(home, cfg, tmp_path, monkeypatch):
    _setup(home, cfg, tmp_path)
    seed_phase(home, "T-due", Phase.READY)
    real_run = subprocess.run

    def hang(argv, *a, **k):
        if "fetch" in argv:
            raise subprocess.TimeoutExpired(argv, k.get("timeout"))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(disp.subprocess, "run", hang)
    result = disp.sync_worktrees(cfg)
    assert any("TimeoutExpired" in v for v in result["errors"].values())

    cfg.min_spawn_interval = 0
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-due" in report.spawned
    assert "sync_worktrees" not in report.hook_errors
