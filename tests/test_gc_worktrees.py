"""T-274: `maestro gc-worktrees` lists leftovers by default and removes only safe ones with --apply."""
from __future__ import annotations

import json
import os

import pytest
from conftest import add_worktree, git, make_origin_and_repo, seed_phase

from maestro import claims, cli
from maestro import dispatcher as disp
from maestro.statemachine import Phase


@pytest.fixture
def board(home, tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    _, repo = make_origin_and_repo(tmp_path, name="target")
    (home / "config.toml").write_text(f'[maestro]\nrepo_path = "{repo}"\n')
    for key, phase in (("D-1", Phase.DONE), ("D-2", Phase.DONE), ("D-3", Phase.DONE),
                       ("I-1", Phase.IMPLEMENTING), ("D-5", Phase.DONE)):
        seed_phase(home, key, phase)
        add_worktree(repo, home, key, f"b-{key.lower()}")
    add_worktree(repo, home, "N-1", "b-n1")  # no snapshot
    return repo


def _run(capsys, *argv):
    rc = cli.main(["gc-worktrees", *argv])
    return rc, capsys.readouterr().out


def _listed(repo):
    out = git("worktree", "list", cwd=repo)
    return getattr(out, "stdout", out)


def test_default_lists_and_removes_nothing(board, home, capsys):
    rc, out = _run(capsys, "--json")
    assert rc == 0
    rep = json.loads(out)
    acts = {e["key"]: e["action"] for e in rep["entries"]}
    assert acts["D-1"] == "would-remove" and acts["I-1"] == "keep" and acts["N-1"] == "keep"
    assert all((home / "worktrees" / k).is_dir() for k in acts)
    rc, out = _run(capsys)
    assert rc == 0 and "D-1: would-remove" in out and "total:" in out


def test_apply_removes_clean_leftover(board, home, capsys):
    rc, _ = _run(capsys, "--apply")
    assert rc == 0
    assert not (home / "worktrees" / "D-1").exists()
    assert str(home / "worktrees" / "D-1") not in str(_listed(board))
    assert (home / "worktrees" / "I-1").is_dir() and (home / "worktrees" / "N-1").is_dir()


def test_live_claim_and_non_terminal_never_removed_even_forced(board, home, capsys):
    claims.write_claim(home, "D-2", os.getpid(), "reconcile-D-2")
    _run(capsys, "--apply", "--force")
    assert (home / "worktrees" / "D-2").is_dir()
    assert (home / "worktrees" / "I-1").is_dir()
    assert (home / "worktrees" / "N-1").is_dir()
    assert not (home / "worktrees" / "D-1").exists()


def test_dirty_skipped_unless_force(board, home, capsys):
    (home / "worktrees" / "D-3" / "scratch.txt").write_text("wip\n")
    git("add", "scratch.txt", cwd=home / "worktrees" / "D-3")
    rc, out = _run(capsys, "--apply")
    assert rc == 0 and (home / "worktrees" / "D-3").is_dir()
    assert any("D-3: skip" in line for line in out.splitlines())
    _run(capsys, "--apply", "--force")
    assert not (home / "worktrees" / "D-3").exists()


def test_non_git_dir_needs_force(board, home, capsys):
    plain = home / "worktrees" / "D-5"
    git("worktree", "remove", "--force", str(plain), cwd=board)
    plain.mkdir()
    (plain / "f").write_text("x")
    _run(capsys, "--apply")
    assert plain.is_dir()
    _run(capsys, "--apply", "--force")
    assert not plain.exists()


def test_human_tag_and_not_agent_verb():
    assert "gc-worktrees" not in disp.AGENT_TOOL_VERBS
    helptext = cli.build_parser().format_help()
    assert "gc-worktrees" in helptext and "[human] list leftover" in helptext


def test_doctor_warning_names_verb(board, home):
    from maestro import health
    from maestro.config import Config
    res = health.check_worktree_leftovers(Config(home=home), 0.0)
    assert "maestro gc-worktrees" in res["detail"]
