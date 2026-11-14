"""`maestro stack`: start a ticket on its single unfinished dependency's branch. Real CLI, real
sweeps and real git over a bare origin; only `gh` (the `open-pr` stub, `FakeVCS`) is mocked."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from maestro import dispatcher as disp
from maestro import event_log, implementing_brief, ops, providers, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo


def _out(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _create(home, key, *, phase=Phase.READY.value, deps=(), kind=None, repo=None):
    deps_line = f"dependsOn: [{', '.join(deps)}]\n" if deps else ""
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n{deps_line}\n## Acceptance criteria\n- [ ] ok\n")
    payload = {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}
    if kind:
        payload["kind"] = kind
    if repo:
        payload["repo"] = repo
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="d")
    snap_mod.rebuild(home, key)


def _push_dep_branch(repo, key="T-1"):
    git("checkout", "-q", "-b", f"maestro/{key}", cwd=repo)
    (repo / f"{key}.txt").write_text(key)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"{key} work", cwd=repo)
    git("push", "-q", "-u", "origin", f"maestro/{key}", cwd=repo)
    tip = _out("rev-parse", "HEAD", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return tip


def _cli(home, *argv):
    return cli_main(["--home", str(home), *argv])


@pytest.fixture
def board(home, cfg, tmp_path):
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    cfg.ready_fast_path = "on"
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nready_fast_path = "on"\n', encoding="utf-8")
    _create(home, "T-1", phase=Phase.IMPLEMENTING.value)
    _create(home, "T-2", deps=["T-1"])
    return repo


def _events(home, key, type_):
    return [e for e in event_log.read(home, key) if e["type"] == type_]


# -- AC1 / AC2: the verb ---------------------------------------------------

def test_stack_records_dep_once(home, board, capsys):
    assert _cli(home, "stack", "T-2") == 0
    assert json.loads(capsys.readouterr().out)["dep"] == "T-1"
    assert snap_mod.load(home, "T-2").stacked_on == "T-1"
    (ev,) = _events(home, "T-2", "TicketStacked")
    assert ev["payload"] == {"dep": "T-1"}

    assert _cli(home, "stack", "T-2") == 0
    assert len(_events(home, "T-2", "TicketStacked")) == 1


def _refused(home, key, capsys, needle):
    before = len(event_log.read(home, key))
    assert _cli(home, "stack", key) == 1
    assert needle in capsys.readouterr().err
    assert len(event_log.read(home, key)) == before


def test_stack_refuses_zero_and_several_unfinished_deps(home, board, capsys):
    _create(home, "T-3")
    _refused(home, "T-3", capsys, "exactly one unfinished dependency")
    _create(home, "T-4", phase=Phase.IMPLEMENTING.value)
    _create(home, "T-5", deps=["T-1", "T-4"])
    _refused(home, "T-5", capsys, "T-1, T-4")


def test_stack_refuses_done_dependency(home, board, capsys):
    event_log.append(home, "T-1", "Finalized", {}, actor="r")
    snap_mod.rebuild(home, "T-1")
    _refused(home, "T-2", capsys, "found 0")


def test_stack_refuses_existing_worktree(home, board, capsys):
    store.worktree_path(home, "T-2").mkdir(parents=True)
    _refused(home, "T-2", capsys, "already has a worktree")


def test_stack_refuses_research_ticket(home, board, capsys):
    _create(home, "T-6", deps=["T-1"], kind="research")
    _refused(home, "T-6", capsys, "research")


def test_stack_refuses_local_mode_and_repo_mismatch(home, board, tmp_path, capsys):
    notes = tmp_path / "notes"
    notes.mkdir()
    other_origin, other = make_origin_and_repo(tmp_path, name="other")
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{board}"\n'
        f'[repos.notes]\npath = "{notes}"\nmode = "local"\n'
        f'[repos.other]\npath = "{other}"\nslug = "o/other"\n', encoding="utf-8")
    _create(home, "T-7", deps=["T-1"], repo="notes")
    _refused(home, "T-7", capsys, "local-mode")
    _create(home, "T-8", deps=["T-1"], repo="other")
    _refused(home, "T-8", capsys, "bound to repo 'other'")


# -- AC3: the sweep ------------------------------------------------------------

def test_sweep_starts_stacked_ticket_on_dep_branch_tip(home, cfg, board):
    tip = _push_dep_branch(board)
    assert _cli(home, "stack", "T-2") == 0

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert snap_mod.load(home, "T-2").phase == Phase.IMPLEMENTING.value
    wt = store.worktree_path(home, "T-2")
    assert _out("rev-parse", "HEAD", cwd=wt) == tip
    assert _out("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "maestro/T-2"


def test_stacked_ticket_stays_blocked_without_dep_branch(home, cfg, board):
    assert _cli(home, "stack", "T-2") == 0
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-2").phase == Phase.READY.value
    assert not store.worktree_path(home, "T-2").exists()


def test_unstacked_ticket_stays_blocked_even_with_dep_branch(home, cfg, board):
    _push_dep_branch(board)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-2").phase == Phase.READY.value
    assert not store.worktree_path(home, "T-2").exists()


# -- AC4: env and open-pr --------------------------------------------------------

GH_STUB = '''#!{py}
import json, os, sys
d = "{d}"
a = sys.argv[1:]
open(os.path.join(d, "gh.log"), "a").write(json.dumps(a) + "\\n")
made = os.path.join(d, "made")
if a[:2] == ["pr", "view"]:
    if not os.path.exists(made):
        sys.exit(1)
    print(json.dumps({{"number": 9, "url": "https://github.com/o/r/pull/9"}}))
elif a[:2] == ["pr", "create"]:
    open(made, "w").write("1")
    print("https://github.com/o/r/pull/9")
'''


def test_env_and_open_pr_use_dep_branch(home, cfg, board, tmp_path, monkeypatch, capsys):
    _push_dep_branch(board)
    assert _cli(home, "stack", "T-2") == 0
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    capsys.readouterr()

    assert _cli(home, "env", "--key", "T-2") == 0
    assert json.loads(capsys.readouterr().out)["base_branch"] == "maestro/T-1"

    bindir, state = tmp_path / "bin", tmp_path / "ghstate"
    bindir.mkdir()
    state.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_STUB.format(py=sys.executable, d=state))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    wt = store.worktree_path(home, "T-2")
    (wt / "b.txt").write_text("b")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "b", cwd=wt)

    assert _cli(home, "open-pr", "T-2", "--title", "T-2: x", "--body", "y") == 0
    rows = [json.loads(line) for line in (state / "gh.log").read_text().splitlines()]
    create = next(r for r in rows if r[:2] == ["pr", "create"])
    assert create[create.index("--base") + 1] == "maestro/T-1"


# -- AC5-AC7: PR lifecycle -----------------------------------------------------

class FakeVCS:
    """The external GitHub boundary: records `pr_ready` and `set_base` calls."""

    def __init__(self):
        self.ready_calls, self.base_calls = [], []

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
                "ci_state": "passing", "failing_checks": [], "draft": True}

    def review_feedback(self, pr_number, repo=None, env=None):
        return []

    def pr_ready(self, pr_number, repo=None, env=None):
        self.ready_calls.append(pr_number)
        return {"ok": True}

    def set_base(self, pr_number, base, repo=None, env=None):
        self.base_calls.append((pr_number, base))
        return {"ok": True}


@pytest.fixture
def pr_ticket(home, cfg, board, monkeypatch):
    """T-2 stacked on T-1, in awaiting-ci with a QA-passed PR #42."""
    fake = FakeVCS()
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)
    assert _cli(home, "stack", "T-2") == 0
    event_log.append(home, "T-2", "PrOpened",
                     {"number": 42, "url": "https://github.com/x/y/pull/42", "draft": True},
                     actor="r")
    event_log.append(home, "T-2", "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
    snap_mod.rebuild(home, "T-2")
    ops.record_qa_verdict(cfg, "T-2", 1, "pass", "looks right")
    event_log.append(home, "T-2", "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    snap_mod.rebuild(home, "T-2")
    return fake


def _finish_dep(home, *, merged):
    event_log.append(home, "T-1", "PrOpened",
                     {"number": 7, "url": "https://github.com/x/y/pull/7", "draft": False},
                     actor="r")
    if merged:
        event_log.append(home, "T-1", "PrUpdated", {"merged": True}, actor="r")
    event_log.append(home, "T-1", "Finalized", {}, actor="r")
    snap_mod.rebuild(home, "T-1")


def test_stacked_pr_not_undrafted_while_dep_open(home, cfg, pr_ticket):
    disp.sync_vcs(cfg, now=1000)
    assert pr_ticket.ready_calls == []
    assert snap_mod.load(home, "T-2").pr_draft is True


def test_dep_merge_retargets_pr_and_routes_to_implementing(home, cfg, pr_ticket, capsys):
    _finish_dep(home, merged=True)
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert pr_ticket.base_calls == [(42, "main")]
    snap = snap_mod.load(home, "T-2")
    assert snap.phase == Phase.IMPLEMENTING.value and snap.stack_outcome == "merged"
    assert implementing_brief.current_round(home, "T-2")[0] == "base-drift"
    assert _cli(home, "env", "--key", "T-2") == 0
    assert json.loads(capsys.readouterr().out)["base_branch"] == "main"

    disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert pr_ticket.base_calls == [(42, "main")]
    assert len(_events(home, "T-2", "StackResolved")) == 1


def test_dep_ending_unmerged_asks_a_human(home, cfg, pr_ticket):
    _finish_dep(home, merged=False)
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(home, "T-2")
    assert snap.phase == Phase.AWAITING_HUMAN.value and snap.stack_outcome == "unmerged"
    assert pr_ticket.base_calls == []
    (question,) = snap.open_questions.values()
    assert "T-1" in question
