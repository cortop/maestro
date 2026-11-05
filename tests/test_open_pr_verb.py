"""`maestro open-pr`: push, create or adopt the draft PR, record PrOpened once. `gh` is the
external boundary and is stubbed with a stateful executable; git is real over a bare origin."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from maestro import cli, event_log, snapshot as snap_mod

import subprocess

from conftest import git, make_origin_and_repo, seed_ticket


def gout(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


KEY = "T-1"
BRANCH = f"maestro/{KEY}"

GH_STUB = r'''#!{py}
import json, os, sys
d = "{d}"
a = sys.argv[1:]
open(os.path.join(d, "gh.log"), "a").write(json.dumps(a) + "\n")
def opt(name):
    return a[a.index(name) + 1] if name in a else None
if a[:2] == ["pr", "view"]:
    p = os.path.join(d, "pr-" + a[2].replace("/", "_"))
    if not os.path.exists(p):
        sys.exit(1)
    print(open(p).read())
elif a[:2] == ["pr", "create"]:
    n = len([f for f in os.listdir(d) if f.startswith("pr-")]) + 1
    open(os.path.join(d, "pr-" + opt("--head").replace("/", "_")), "w").write(
        json.dumps({{"number": n, "url": "https://github.com/o/r/pull/%d" % n}}))
    print("https://github.com/o/r/pull/%d" % n)
'''


@pytest.fixture
def env(home, tmp_path, monkeypatch):
    origin, wt = make_origin_and_repo(home / "worktrees", name=KEY)
    seed_ticket(home, KEY, "open pr", phase="implementing")
    git("checkout", "-q", "-b", BRANCH, cwd=wt)
    bindir, state = tmp_path / "bin", tmp_path / "ghstate"
    bindir.mkdir()
    state.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB.format(py=sys.executable, d=state))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return wt, state


def _gh_calls(state, sub):
    path = state / "gh.log"
    rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r[:2] == ["pr", sub]]


def _commit(wt, name):
    (wt / name).write_text(name)
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", name, cwd=wt)
    return gout("rev-parse", "HEAD", cwd=wt).strip()


def _opened(home):
    return [e for e in event_log.read(home, KEY) if e["type"] == "PrOpened"]


def test_open_pr_idempotent(home, env, capsys):
    wt, state = env
    _commit(wt, "a.txt")
    argv = ["--home", str(home), "open-pr", KEY, "--title", "T-1: thing", "--body", "why"]
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recorded"] is True and out["opened"][0]["number"] == 1
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["recorded"] is False
    assert len(_gh_calls(state, "create")) == 1
    assert len(_opened(home)) == 1
    snap = snap_mod.load(home, KEY)
    assert snap.pr_number == 1 and snap.pr_draft is True
    create = _gh_calls(state, "create")[0]
    assert "--draft" in create and create[create.index("--title") + 1] == "T-1: thing"
    assert gout("ls-remote", "--heads", "origin", BRANCH, cwd=wt).strip()


def test_open_pr_stack_root_first(home, env, capsys):
    wt, state = env
    c1 = _commit(wt, "a.txt")
    _commit(wt, "b.txt")
    stack = [{"title": "T-1: part a (1/2)", "body": "body a", "sha": c1},
             {"title": "T-1: part b (2/2)", "body": "body b"}]
    argv = ["--home", str(home), "open-pr", KEY, "--stack", json.dumps(stack)]
    assert cli.main(argv) == 0
    capsys.readouterr()
    creates = _gh_calls(state, "create")
    heads = [c[c.index("--head") + 1] for c in creates]
    bases = [c[c.index("--base") + 1] for c in creates]
    titles = [c[c.index("--title") + 1] for c in creates]
    bodies = [c[c.index("--body") + 1] for c in creates]
    assert heads == [f"{BRANCH}-1", BRANCH]
    assert bases == ["main", f"{BRANCH}-1"]
    assert titles == ["T-1: part a (1/2)", "T-1: part b (2/2)"]
    assert bodies == ["body a", "body b"]
    opened = _opened(home)
    assert [e["payload"]["stack"]["index"] for e in opened] == [0, 1]
    assert opened[1]["payload"]["stack"]["base"] == f"{BRANCH}-1"
    snap = snap_mod.load(home, KEY)
    assert snap.pr_number == 1 and [e["index"] for e in snap.pr_stack] == [0, 1]
    # entry 0 holds only the first commit
    assert gout("ls-tree", "--name-only", f"origin/{BRANCH}-1", cwd=wt).split() == ["README.md", "a.txt"]
    # re-run: nothing new
    assert cli.main(argv) == 0
    assert len(_gh_calls(state, "create")) == 2 and len(_opened(home)) == 2
