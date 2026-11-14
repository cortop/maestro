"""T-272: doctor probes each worktree once, in parallel, and the three worktree checks keep their output."""
from __future__ import annotations

import subprocess
import threading

import pytest
from conftest import add_worktree, git, make_origin_and_repo

from maestro import health, ops, store
from maestro.config import Config

CHECKS = (health.check_worktree_health, health.check_worktree_witness, health.check_worktree_branch)


@pytest.fixture
def board(home, tmp_path):
    _, repo = make_origin_and_repo(tmp_path, name="target")
    cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
    healthy = add_worktree(repo, home, "A-1", "b-a1")
    (ops._git_dir(healthy) / ops._WORKTREE_COMPLETE_MARKER).write_text("ok")
    add_worktree(repo, home, "B-2", "b-b2")                 # witness-less
    noindex = add_worktree(repo, home, "C-3", "b-c3")
    (ops._git_dir(noindex) / "index").unlink()               # no index
    detached = add_worktree(repo, home, "D-4", "b-d4")
    git("checkout", "-q", "--detach", cwd=detached)          # detached HEAD
    return cfg


def _run(cfg):
    return {c.__name__: c(cfg, 0.0) for c in CHECKS}


def test_each_probe_runs_at_most_once_per_worktree(board, monkeypatch):
    calls = []
    real = subprocess.run

    def counting(cmd, *a, **kw):
        calls.append((cmd[2], tuple(cmd[3:])))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counting)
    health.run_checks(board, 0.0)
    wt_calls = [c for c in calls if c[0].startswith(str(board.home / "worktrees"))]
    assert len(wt_calls) == len(set(wt_calls))
    index_calls = [c for c in wt_calls if c[1][:2] == ("rev-parse", "--git-path")]
    assert len(index_calls) == 4


def test_results_unchanged_for_each_worktree_shape(board):
    r = health.run_checks(board, 0.0)
    by = {c["name"]: c for c in r}
    assert by["worktree_health"]["status"] == "warn"
    assert by["worktree_health"]["broken"] == [{"key": "C-3", "reason": "no index"}]
    assert by["worktree_witness"]["witness_less"] == [
        {"key": "B-2", "reason": "no witness (predates T-81)"},
        {"key": "D-4", "reason": "no witness (predates T-81)"}]
    assert by["worktree_branch"]["detached"] == [{"key": "D-4", "reason": (
        "HEAD is detached -- no branch to commit onto; `maestro cmd D-4 retry` re-adopts it")}]


def test_each_check_alone_matches_full_run(board):
    full = {c["name"]: c for c in health.run_checks(board, 0.0)}
    for check in CHECKS:
        alone = check(board, 0.0)
        assert alone == full[alone["name"]]


def test_probe_error_is_isolated_to_its_worktree(board, monkeypatch):
    real = ops.worktree_branch

    def flaky(wt, **kw):
        if wt.name == "B-2":
            raise store.MaestroError("boom")
        return real(wt, **kw)

    monkeypatch.setattr(ops, "worktree_branch", flaky)
    for result in (health.check_worktree_branch(board, 0.0),
                   {c["name"]: c for c in health.run_checks(board, 0.0)}["worktree_branch"]):
        keys = [d["key"] for d in result["detached"]]
        assert keys == ["B-2", "D-4"]
        assert result["detached"][0]["reason"] == "branch probe error: boom"


def test_entries_sorted_whatever_the_finish_order(board, monkeypatch):
    # Later keys finish first: earlier ones wait on the last one's probe.
    done = threading.Event()
    real = ops.worktree_branch

    def slow(wt, **kw):
        if wt.name != "D-4":
            done.wait(5)
        try:
            return real(wt, **kw)
        finally:
            if wt.name == "D-4":
                done.set()

    monkeypatch.setattr(ops, "worktree_branch", slow)
    monkeypatch.setattr(ops, "worktree_witness_status",
                        lambda wt, **kw: {"backfillable": True})
    assert [w["key"] for w in health.check_worktree_witness(board, 0.0)["witness_less"]] == [
        "A-1", "B-2", "C-3", "D-4"]
    monkeypatch.setattr(ops, "worktree_branch", lambda wt, **kw: slow(wt, **kw) and None)
    assert [d["key"] for d in health.check_worktree_branch(board, 0.0)["detached"]] == [
        "A-1", "B-2", "C-3", "D-4"]
