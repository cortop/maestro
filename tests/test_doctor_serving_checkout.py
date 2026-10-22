"""`maestro doctor` serving_checkout: WARN on a dirty/detached/off-branch serving checkout or stray command symlinks."""
from __future__ import annotations

import pytest

from conftest import git, make_origin_and_repo, run_doctor
from maestro import health


@pytest.fixture
def serving(tmp_path, monkeypatch):
    _, repo = make_origin_and_repo(tmp_path, name="serving")
    git("remote", "set-head", "origin", "main", cwd=repo)
    cmds = tmp_path / "cmds"
    cmds.mkdir()
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(cmds))
    monkeypatch.setattr(health, "_maestro_self_root", lambda: repo)
    return repo, cmds


def _check(home):
    _, payload = run_doctor(home)
    return next(c for c in payload["checks"] if c["name"] == "serving_checkout")


def _link(cmds, target):
    (cmds / "maestro-reconcile-ready.md").symlink_to(target)


def test_ok_clean_and_non_git(home, serving, tmp_path, monkeypatch):
    repo, cmds = serving
    (repo / "cmd.md").write_text("x\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "cmd", cwd=repo)
    _link(cmds, repo / "cmd.md")
    assert _check(home)["status"] == "ok"
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(health, "_maestro_self_root", lambda: plain)
    check = _check(home)
    assert check["status"] == "ok" and "skipped" in check["detail"]


def test_warns_dirty_detached_off_branch(home, serving):
    repo, _ = serving
    (repo / "dirty.txt").write_text("x\n")
    check = _check(home)
    assert check["status"] == "warn" and "dirty" in check["detail"]
    (repo / "dirty.txt").unlink()
    git("checkout", "-q", "-b", "feature", cwd=repo)
    check = _check(home)
    assert check["status"] == "warn" and "feature" in check["detail"]
    git("checkout", "-q", "--detach", cwd=repo)
    check = _check(home)
    assert check["status"] == "warn" and "detached" in check["detail"]


def test_warns_command_symlink_outside(home, serving, tmp_path):
    _, cmds = serving
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("x\n")
    _link(cmds, elsewhere)
    check = _check(home)
    assert check["status"] == "warn" and "outside" in check["detail"]
