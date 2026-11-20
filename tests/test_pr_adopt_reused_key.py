"""`open-pr` adoption rules for a reused key, and max+1 auto-key allocation. Only `gh` is stubbed."""
from __future__ import annotations

import json

from maestro import cli, inbox, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.config import load

from test_open_pr_verb import BRANCH, KEY, _commit, _gh_calls, _opened, env, gout  # noqa: F401


def _put(state, number, st, sha):
    (state / f"pr-{BRANCH.replace('/', '_')}").write_text(json.dumps(
        {"number": number, "url": f"https://github.com/o/r/pull/{number}", "state": st, "headRefOid": sha}))


def _run(home, capsys):
    assert cli.main(["--home", str(home), "open-pr", KEY, "--title", "T: x", "--body", "b"]) == 0
    return json.loads(capsys.readouterr().out)


def test_merged_pr_at_other_sha_creates_new_pr(home, env, capsys):
    wt, state = env
    _commit(wt, "a.txt")
    _put(state, 4, "MERGED", "0" * 40)
    out = _run(home, capsys)
    assert len(_gh_calls(state, "create")) == 1
    assert out["opened"][0]["number"] != 4
    assert _opened(home)[-1]["payload"]["number"] == out["opened"][0]["number"]


def test_open_pr_is_adopted(home, env, capsys):
    wt, state = env
    sha = _commit(wt, "a.txt")
    _put(state, 7, "OPEN", "1" * 40)
    out = _run(home, capsys)
    assert out["opened"][0]["number"] == 7
    assert _gh_calls(state, "create") == []
    assert snap_mod.load(home, KEY).pr_number == 7


def test_merged_same_sha_adopted_closed_never(home, env, capsys):
    wt, state = env
    sha = _commit(wt, "a.txt")
    _put(state, 9, "MERGED", sha)
    assert _run(home, capsys)["opened"][0]["number"] == 9
    assert _gh_calls(state, "create") == []
    _put(state, 9, "CLOSED", sha)
    out = _run(home, capsys)
    assert len(_gh_calls(state, "create")) == 1
    assert out["opened"][0]["number"] != 9


def test_rerun_corrects_wrong_recorded_pr(home, env, capsys):
    wt, state = env
    _commit(wt, "a.txt")
    _put(state, 4, "OPEN", "2" * 40)
    _run(home, capsys)
    assert snap_mod.load(home, KEY).pr_number == 4
    _put(state, 4, "MERGED", "2" * 40)  # stale: head differs from the branch
    out = _run(home, capsys)
    new = out["opened"][0]["number"]
    assert new != 4 and out["recorded"] is True
    assert [e["payload"]["number"] for e in _opened(home)] == [4, new]
    assert snap_mod.load(home, KEY).pr_number == new


def test_auto_key_is_one_above_highest(home):
    for n in (1, 2, 5):
        (home / "tickets" / f"X-{n}").mkdir(parents=True)
    (home / "tickets" / "XY-9").mkdir(parents=True)
    assert disp._auto_key(home, prefix="X") == "X-6"
    assert disp._auto_key(home, prefix="Q") == "Q-1"


def test_mint_loop_uses_max_plus_one(home):
    for n in (1, 2, 5):
        (home / "tickets" / f"X-{n}").mkdir(parents=True)
    inbox.append_new(home, "thing", args={"acs": ["one"]}, prefix="X")
    assert disp.mint_new_tickets(load(home)) == ["X-6"]
