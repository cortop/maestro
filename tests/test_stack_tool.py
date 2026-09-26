"""T-132: use the Graphite CLI (`gt`) to build/maintain PR stacks when it's
available for a repo, leaving the existing git/gh flow byte-for-byte
unchanged when it isn't.

`repos.resolve_stack_tool` is the deterministic (PATH lookup + marker-file
read, no `gt` subprocess of its own) detector `maestro env --key` surfaces as
`stack_tool`. `config.load()` fails closed on an unrecognized
`[repos.<name>] stack_tool` value, same posture as `language`/
`base_drift_policy`. Once a gt-managed stack's tracked entry merges,
`ops.check_merged` queues a restack (`RestackQueued`); `dispatcher.
sync_restacks` runs it as a detached subprocess (the `sync_test_runs`
shape) and, on a clean exit, retargets every remaining PR's GitHub base and
records `RestackCompleted`. A `git`-stack_tool ticket never queues one, so
it never runs `gt` and never has anything here that could force-push.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from maestro import claims, config as config_mod, event_log, ops, providers
from maestro import repos as repos_mod, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


# ---------------------------------------------------------------------------
# AC2: an unrecognized stack_tool value fails config.load() closed.
# ---------------------------------------------------------------------------

def test_repos_table_stack_tool_unrecognized_fails_config_load_closed(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nstack_tool = "bogus"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError, match="stack_tool must be one of"):
        config_mod.load(str(home))


def test_repos_table_stack_tool_unset_resolves_to_auto(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n[repos.alpha]\npath = "/repo/alpha"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["stack_tool"] is None
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.stack_tool == "auto"


def test_repos_table_stack_tool_git_forces_git(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nstack_tool = "git"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.stack_tool == "git"


# ---------------------------------------------------------------------------
# AC1: repos.resolve_stack_tool -- deterministic PATH + marker-file detection.
# ---------------------------------------------------------------------------

def _fake_gt_dir(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    gt_log = tmp_path / "gt.log"
    gt_stub = bin_dir / "gt"
    gt_stub.write_text(f'#!/bin/sh\necho "$@" >> "{gt_log}"\nexit 0\n')
    gt_stub.chmod(0o755)
    return bin_dir, gt_log


def _graphite_init(repo_dir):
    (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git" / ".graphite_repo_config").write_text("{}\n", encoding="utf-8")


def test_resolve_stack_tool_forced_git_never_probes_path(tmp_path):
    binding = repos_mod.RepoBinding(name="x", path=str(tmp_path), slug=None,
                                    base_branch="main", branch_prefix="maestro/",
                                    stack_tool="git")
    _graphite_init(tmp_path)
    assert repos_mod.resolve_stack_tool(binding) == "git"


def test_resolve_stack_tool_auto_no_gt_on_path_is_git(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    _graphite_init(tmp_path)
    binding = repos_mod.RepoBinding(name="x", path=str(tmp_path), slug=None,
                                    base_branch="main", branch_prefix="maestro/",
                                    stack_tool="auto")
    assert repos_mod.resolve_stack_tool(binding) == "git"


def test_resolve_stack_tool_auto_gt_on_path_but_not_initialized_is_git(tmp_path, monkeypatch):
    bin_dir, _log = _fake_gt_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    binding = repos_mod.RepoBinding(name="x", path=str(tmp_path), slug=None,
                                    base_branch="main", branch_prefix="maestro/",
                                    stack_tool="auto")
    assert repos_mod.resolve_stack_tool(binding) == "git"


def test_resolve_stack_tool_auto_gt_on_path_and_initialized_is_gt(tmp_path, monkeypatch):
    bin_dir, _log = _fake_gt_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _graphite_init(tmp_path)
    binding = repos_mod.RepoBinding(name="x", path=str(tmp_path), slug=None,
                                    base_branch="main", branch_prefix="maestro/",
                                    stack_tool="auto")
    assert repos_mod.resolve_stack_tool(binding) == "gt"
    assert not _log_exists_with_content(_log)  # deterministic -- never shells `gt` itself


def _log_exists_with_content(log_path):
    return log_path.exists() and log_path.read_text().strip() != ""


# ---------------------------------------------------------------------------
# AC1: `maestro env --key` reports stack_tool -- real CLI, fake `gt` on PATH.
# ---------------------------------------------------------------------------

def test_maestro_env_key_reports_gt_with_fake_gt_on_path_and_graphite_init(home, monkeypatch, capsys):
    repo_dir = home.parent / "repo"
    repo_dir.mkdir(exist_ok=True)
    _graphite_init(repo_dir)
    bin_dir, _log = _fake_gt_dir(home.parent)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    (home / "config.toml").write_text(
        f'[maestro]\n\n[repos.alpha]\npath = "{repo_dir}"\ndefault = true\n',
        encoding="utf-8")
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\n\n## Intent\nx\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    rc = cli_main(["--home", str(home), "env", "--key", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"stack_tool": "gt"' in out


def test_maestro_env_key_reports_git_with_no_gt_on_path(home, monkeypatch, capsys):
    repo_dir = home.parent / "repo2"
    repo_dir.mkdir(exist_ok=True)
    _graphite_init(repo_dir)
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    (home / "config.toml").write_text(
        f'[maestro]\n\n[repos.alpha]\npath = "{repo_dir}"\ndefault = true\n',
        encoding="utf-8")
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\n\n## Intent\nx\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    rc = cli_main(["--home", str(home), "env", "--key", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"stack_tool": "git"' in out


def test_maestro_env_key_reports_git_when_repo_forces_it(home, monkeypatch, capsys):
    repo_dir = home.parent / "repo3"
    repo_dir.mkdir(exist_ok=True)
    _graphite_init(repo_dir)
    bin_dir, _log = _fake_gt_dir(home.parent)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    (home / "config.toml").write_text(
        f'[maestro]\n\n[repos.alpha]\npath = "{repo_dir}"\ndefault = true\n'
        'stack_tool = "git"\n',
        encoding="utf-8")
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\n\n## Intent\nx\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1"}, actor="d")
    snap_mod.rebuild(home, "T-1")

    rc = cli_main(["--home", str(home), "env", "--key", "T-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"stack_tool": "git"' in out


# ---------------------------------------------------------------------------
# AC3: the implementing skill's approved-split/conflict-round instructions
# branch on STACK_TOOL -- gt commands present, git commands still there
# word for word (test_reconcile_skill.py's own byte-identity test already
# covers the two copies mirroring each other).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _implementing_skill_texts() -> list[str]:
    return [
        (_REPO_ROOT / ".claude" / "commands" / "maestro-reconcile-implementing.md").read_text(),
        (_REPO_ROOT / "skills" / "maestro-reconcile-implementing.md").read_text(),
    ]


def test_implementing_skill_branches_approved_split_and_conflict_round_on_stack_tool():
    for text in _implementing_skill_texts():
        assert "STACK_TOOL" in text
        # gt path -- conflict round
        assert "gt -C <WT> sync" in text
        assert "gt -C <WT> restack" in text
        assert "gt -C <WT> continue" in text
        # gt path -- approved split
        assert 'gt -C <WT> create "<PREFIX>$KEY-1"' in text
        assert "gt -C <WT> submit --stack" in text
        # the git path's own literal commands are still present, word for word
        assert 'git -C <WT> fetch -q origin "<PREFIX>$KEY"' in text
        assert 'git -C <WT> merge -q --ff-only "origin/<PREFIX>$KEY"' in text
        assert 'git -C <WT> rebase "origin/<BASE>"' in text
        assert 'git -C <WT> rebase --continue' in text
        assert 'git -C <WT> branch "<PREFIX>$KEY-1" <sha of entry 1' in text
        assert 'git -C <WT> push -q -u origin "<PREFIX>$KEY-1"' in text
        assert 'gh pr create --repo "<SLUG>" --base "<BASE>" --head "<PREFIX>$KEY-1"' in text


# ---------------------------------------------------------------------------
# AC4/AC5: the restack-after-merge sweep, and a git-tool ticket's abstention.
# ---------------------------------------------------------------------------

class FakeVCS:
    """The only mock: the external GitHub boundary. `set_base` records every
    retarget call so the test can assert it directly, standing in for the
    fact that a fake `gt` on PATH does nothing to GitHub of its own."""

    def __init__(self, statuses):
        self.statuses = {k: dict(v) for k, v in statuses.items()}
        self.set_base_calls: list[tuple[int, str]] = []

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return dict(self.statuses[pr_number])

    def review_feedback(self, pr_number, repo=None, env=None):
        return []

    def pr_ready(self, pr_number, repo=None, env=None):
        return {"ok": False, "error": "unknown"}

    def rerun_failed(self, pr_number, head_sha, repo=None, env=None):
        return {"ok": False}

    def failed_log_tail(self, run_id, max_bytes=2048, repo=None, env=None):
        return ""

    def reply_to_review_comment(self, pr_number, comment_id, body, repo=None, env=None):
        return {"ok": False, "error": "unknown"}

    def comment_pr(self, pr_number, body, repo=None, env=None):
        return {"ok": False, "error": "unknown"}

    def set_base(self, pr_number, base, repo=None, env=None):
        self.set_base_calls.append((pr_number, base))
        return {"ok": True}


def _use_fake(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _seed_stack(cfg, key, *, n=3):
    """A 3-entry approved split, PR numbers 100/101/102, in `awaiting-ci` --
    entry 0 the active poll target, mirroring exactly what the implementing
    skill's "Approved split" block appends."""
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    for i in range(n):
        pr = 100 + i
        base = "main" if i == 0 else f"maestro/{key}-{i}"
        event_log.append(cfg.home, key, "PrOpened", {
            "number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": True,
            "stack": {"index": i, "total": n, "branch": f"maestro/{key}-{i + 1}", "base": base},
        }, actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.AWAITING_CI.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _wait_until_dead(pid, *, timeout=10.0):
    import time
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not claims.pid_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} still alive after {timeout}s")


def test_gt_stack_restacks_and_retargets_bases_after_a_merge(cfg, monkeypatch, tmp_path):
    key = "T-9"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _graphite_init(repo_dir)
    cfg.repo_path = str(repo_dir)
    bin_dir, gt_log = _fake_gt_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    store.worktree_path(cfg.home, key).mkdir(parents=True)

    fake = FakeVCS({
        100: {"state": "MERGED", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "unknown", "failing_checks": [], "draft": False},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
        102: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha102",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
    })
    _use_fake(cfg, monkeypatch, fake)
    _seed_stack(cfg, key, n=3)
    assert repos_mod.resolve_stack_tool(repos_mod.resolve(cfg, cfg.home, key)) == "gt"

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)  # detects the merge, queues + starts the restack

    events = event_log.read(cfg.home, key)
    queued = [e for e in events if e["type"] == "RestackQueued"]
    assert len(queued) == 1 and queued[0]["payload"] == {"stack_index": 0}
    assert snap_mod.load(cfg.home, key).pending_restack_index == 0

    claim = claims.read_claim(cfg.home, key)
    assert claim is not None and claim.get("kind") == "restack"
    _wait_until_dead(claim["pid"])

    disp.dispatch(cfg, sessions, now=1001)  # folds the finished restack subprocess

    events = event_log.read(cfg.home, key)
    completed = [e for e in events if e["type"] == "RestackCompleted"]
    assert len(completed) == 1
    assert completed[0]["payload"] == {
        "stack_index": 0,
        "retargeted": [{"number": 101, "base": "main"},
                       {"number": 102, "base": f"maestro/{key}-2"}],
    }
    assert fake.set_base_calls == [(101, "main"), (102, f"maestro/{key}-2")]

    snap = snap_mod.load(cfg.home, key)
    assert snap.pending_restack_index is None
    entry1 = next(e for e in snap.pr_stack if e["index"] == 1)
    entry2 = next(e for e in snap.pr_stack if e["index"] == 2)
    assert entry1["base"] == "main"
    assert entry2["base"] == f"maestro/{key}-2"
    assert claims.read_claim(cfg.home, key) is None  # restack claim released once folded

    # The fake `gt` really ran (sync, restack, submit), never anything force-pushy of its own.
    log_lines = gt_log.read_text().splitlines()
    assert any(l.startswith("sync") for l in log_lines)
    assert any(l.startswith("restack") for l in log_lines)
    assert any(l.startswith("submit") for l in log_lines)


def test_git_stack_tool_ticket_never_queues_a_restack_or_touches_gt(cfg, monkeypatch, tmp_path):
    """AC5: a `git`-stack_tool ticket's merge advances the stack exactly as
    before T-132 -- no RestackQueued, no restack claim, no `gt` subprocess
    ever launched. Nothing in this codepath COULD force-push either: the only
    place that ever shells `gt`/pushes on this path is `sync_restacks`,
    gated behind `pending_restack_index`, which git-stack_tool tickets never
    set."""
    key = "T-9"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _graphite_init(repo_dir)  # even Graphite-initialized -- the repo forces "git" below
    cfg.repo_path = str(repo_dir)
    cfg.repos["default"] = {"path": str(repo_dir), "default": True, "stack_tool": "git"}
    bin_dir, gt_log = _fake_gt_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    store.worktree_path(cfg.home, key).mkdir(parents=True)

    fake = FakeVCS({
        100: {"state": "MERGED", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "unknown", "failing_checks": [], "draft": False},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
        102: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha102",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
    })
    _use_fake(cfg, monkeypatch, fake)
    _seed_stack(cfg, key, n=3)
    assert repos_mod.resolve_stack_tool(repos_mod.resolve(cfg, cfg.home, key)) == "git"

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    disp.dispatch(cfg, sessions, now=1001)

    events = event_log.read(cfg.home, key)
    assert [e for e in events if e["type"] == "RestackQueued"] == []
    assert [e for e in events if e["type"] == "RestackCompleted"] == []
    assert snap_mod.load(cfg.home, key).pending_restack_index is None
    assert claims.read_claim(cfg.home, key) is None
    assert fake.set_base_calls == []
    assert not gt_log.exists()  # the fake `gt` was never even invoked
    # the merge still advanced the stack normally -- T-132 changes nothing here
    assert snap_mod.load(cfg.home, key).pr_number == 101
