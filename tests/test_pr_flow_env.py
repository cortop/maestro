"""`maestro open-pr` on a repo with a `token_env` credential: the `GH_TOKEN` overlay is
merged into the ambient env, so `gh` is still found on PATH. `gh` is the external
boundary and is stubbed; git is real over a bare origin."""
from __future__ import annotations

import json
import os
import stat
import sys

from maestro import cli, event_log

from conftest import git, make_origin_and_repo, seed_ticket

KEY = "T-1"
BRANCH = f"maestro/{KEY}"

GH_STUB = r'''#!{py}
import json, os, sys
d = "{d}"
a = sys.argv[1:]
open(os.path.join(d, "gh.log"), "a").write(json.dumps({{"argv": a, "token": os.environ.get("GH_TOKEN")}}) + "\n")
p = os.path.join(d, "pr")
if a[:2] == ["pr", "view"]:
    if not os.path.exists(p):
        sys.exit(1)
    print(open(p).read())
elif a[:2] == ["pr", "create"]:
    open(p, "w").write(json.dumps({{"number": 1, "url": "https://github.com/o/r/pull/1"}}))
'''


def test_open_pr_with_token_env_keeps_path(home, tmp_path, monkeypatch, capsys):
    (home / "config.toml").write_text(
        "[maestro]\n\n[repos.app]\ndefault = true\n"
        f'path = "{home / "worktrees" / KEY}"\nslug = "o/r"\ntoken_env = "APP_GH_TOKEN"\n',
        encoding="utf-8")
    _, wt = make_origin_and_repo(home / "worktrees", name=KEY)
    seed_ticket(home, KEY, "open pr", phase="implementing")
    git("checkout", "-q", "-b", BRANCH, cwd=wt)
    (wt / "a.txt").write_text("a")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "a", cwd=wt)

    bindir, state = tmp_path / "bin", tmp_path / "ghstate"
    bindir.mkdir()
    state.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB.format(py=sys.executable, d=state))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("APP_GH_TOKEN", "tok-app")

    argv = ["--home", str(home), "open-pr", KEY, "--title", "T-1: thing", "--body", "why"]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True

    calls = [json.loads(l) for l in (state / "gh.log").read_text().splitlines()]
    assert [c["argv"][:2] for c in calls] == [["pr", "view"], ["pr", "create"], ["pr", "view"]]
    assert {c["token"] for c in calls} == {"tok-app"}
    assert [e["payload"]["number"] for e in event_log.read(home, KEY)
            if e["type"] == "PrOpened"] == [1]
