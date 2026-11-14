"""T-273: doctor decides from the snapshot phase that a worktree is a leftover and runs no git for it."""
from __future__ import annotations

import subprocess

import pytest
from conftest import add_worktree, git, make_origin_and_repo, seed_phase

from maestro import health
from maestro.config import Config
from maestro.statemachine import Phase
from maestro.tui import checks as tui_checks


@pytest.fixture
def board(home, tmp_path):
    _, repo = make_origin_and_repo(tmp_path, name="target")
    cfg = Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)
    for key, phase in (("D-1", Phase.DONE), ("D-2", Phase.DONE), ("I-3", Phase.IMPLEMENTING)):
        seed_phase(home, key, phase)
        add_worktree(repo, home, key, f"b-{key.lower()}")
    add_worktree(repo, home, "N-4", "b-n4")  # no snapshot
    git("checkout", "-q", "--detach", cwd=home / "worktrees" / "I-3")
    git("checkout", "-q", "--detach", cwd=home / "worktrees" / "D-1")
    return cfg


def _wt_calls(cfg, monkeypatch):
    calls, real = [], subprocess.run

    def counting(cmd, *a, **kw):
        calls.append(" ".join(map(str, cmd)))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", counting)
    results = {c["name"]: c for c in health.run_checks(cfg, 0.0)}
    return calls, results


def test_no_git_runs_in_a_done_ticket_worktree(board, monkeypatch):
    calls, _ = _wt_calls(board, monkeypatch)
    wt = str(board.home / "worktrees")
    assert not [c for c in calls if f"{wt}/D-1" in c or f"{wt}/D-2" in c]
    assert [c for c in calls if f"{wt}/I-3" in c]


def test_leftovers_warn_with_count_and_keys(board):
    r = {c["name"]: c for c in health.run_checks(board, 0.0)}["worktree_leftovers"]
    assert r["status"] == "warn" and r["keys"] == ["D-1", "D-2"]
    assert "2" in r["detail"] and "D-1" in r["detail"] and "D-2" in r["detail"]
    assert tui_checks.culprit_keys(r) == ["D-1", "D-2"]


def test_ok_when_no_leftovers(home):
    r = health.check_worktree_leftovers(Config(home=home), 0.0)
    assert r["status"] == "ok" and r["keys"] == []


def test_done_worktree_absent_from_the_three_checks(board):
    by = {c["name"]: c for c in health.run_checks(board, 0.0)}
    assert [d["key"] for d in by["worktree_branch"]["detached"]] == ["I-3"]  # D-1 is detached too
    listed = {e["key"] for e in by["worktree_witness"]["witness_less"] + by["worktree_health"]["broken"]}
    assert not listed & {"D-1", "D-2"}


def test_non_terminal_and_snapshotless_are_probed(board):
    probes = dict(health._gather_worktree_probes(board, ("witness",)))
    assert set(probes) == {"I-3", "N-4"}
    assert health.worktree_is_leftover(board.home, "D-1")
    assert not health.worktree_is_leftover(board.home, "N-4")
    assert not health.worktree_is_leftover(board.home, "I-3")
