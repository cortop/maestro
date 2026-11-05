"""`maestro sync-base`: remote PR branch first, then base (merge when pushed, rebase when not);
the gt path never runs plain git rebase/merge. Real git over a real bare origin."""
from __future__ import annotations

import json
import os
import shutil
import stat

from maestro import cli, repos as repos_mod

import subprocess

from conftest import git, make_origin_and_repo, seed_ticket


def gout(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


KEY = "T-1"
BRANCH = f"maestro/{KEY}"


def _setup(home, tmp_path):
    origin, wt = make_origin_and_repo(home / "worktrees", name=KEY)
    seed_ticket(home, KEY, "sync", phase="implementing")
    git("checkout", "-q", "-b", BRANCH, cwd=wt)
    return origin, wt


def _other_clone(tmp_path, origin, name="other"):
    c = tmp_path / name
    git("clone", "-q", str(origin), str(c), cwd=tmp_path)
    git("config", "user.email", "o@example.com", cwd=c)
    git("config", "user.name", "O", cwd=c)
    return c


def _commit(repo, name, text="x\n", branch=None):
    (repo / name).write_text(text)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"add {name}", cwd=repo)
    if branch:
        git("push", "-q", "origin", f"HEAD:{branch}", cwd=repo)


def _run(home, capsys, verb="sync-base"):
    rc = cli.main(["--home", str(home), verb, KEY])
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else {}


def test_sync_base_merge_and_rebase(home, tmp_path, capsys):
    origin, wt = _setup(home, tmp_path)
    other = _other_clone(tmp_path, origin)

    # never pushed -> rebase onto base
    _commit(wt, "mine.txt")
    _commit(other, "base1.txt", branch="main")
    rc, res = _run(home, capsys)
    assert rc == 0 and res["status"] == "clean" and res["mode"] == "rebase"
    assert (wt / "base1.txt").exists()
    assert gout("rev-list", "--merges", "--count", "HEAD", cwd=wt).strip() == "0"

    # pushed -> the remote branch tip is incorporated first, then base is merged in
    git("push", "-q", "-u", "origin", BRANCH, cwd=wt)
    git("fetch", "-q", "origin", cwd=other)
    git("checkout", "-q", "-b", "tmp", f"origin/{BRANCH}", cwd=other)
    _commit(other, "bot.txt", branch=BRANCH)
    git("checkout", "-q", "main", cwd=other)
    git("pull", "-q", cwd=other)
    _commit(other, "base2.txt", branch="main")
    rc, res = _run(home, capsys)
    assert rc == 0 and res["status"] == "clean" and res["mode"] == "merge"
    assert (wt / "bot.txt").exists() and (wt / "base2.txt").exists()

    # conflicting base change on a pushed branch -> conflicted, left in the tree
    _commit(wt, "clash.txt", "mine\n")
    git("push", "-q", "origin", BRANCH, cwd=wt)
    git("pull", "-q", cwd=other)
    _commit(other, "clash.txt", "theirs\n", branch="main")
    rc, res = _run(home, capsys)
    assert rc == 0 and res["status"] == "conflicted" and res["mode"] == "merge"
    assert res["conflicted_files"] == ["clash.txt"]
    assert res["continue_command"].endswith("commit --no-edit")
    assert (wt / ".git").exists() and "<<<<<<<" in (wt / "clash.txt").read_text()


def test_sync_base_conflicted_rebase_reports_continue(home, tmp_path, capsys):
    origin, wt = _setup(home, tmp_path)
    other = _other_clone(tmp_path, origin)
    _commit(wt, "clash.txt", "mine\n")
    _commit(other, "clash.txt", "theirs\n", branch="main")
    rc, res = _run(home, capsys)
    assert res["status"] == "conflicted" and res["mode"] == "rebase"
    assert res["conflicted_files"] == ["clash.txt"]
    assert res["continue_command"].endswith("rebase --continue")


def test_sync_base_gt_path(home, tmp_path, capsys, monkeypatch):
    origin, wt = _setup(home, tmp_path)
    other = _other_clone(tmp_path, origin)
    _commit(other, "base1.txt", branch="main")
    real_git = shutil.which("git")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for name, body in (("git", f'echo "git $@" >> "{log}"\nexec {real_git} "$@"\n'),
                       ("gt", f'echo "gt $@" >> "{log}"\n')):
        p = bindir / name
        p.write_text(f"#!/bin/sh\n{body}")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(repos_mod, "resolve_stack_tool", lambda binding: "gt")

    rc, res = _run(home, capsys)
    assert rc == 0 and res["status"] == "clean" and res["mode"] == "gt"
    calls = log.read_text().splitlines()
    assert [c.split()[0] + " " + c.split()[3] for c in calls if c.startswith("gt")] == ["gt sync", "gt restack"]
    assert not any(" rebase" in c or " merge" in c or " fetch" in c for c in calls if c.startswith("git"))
