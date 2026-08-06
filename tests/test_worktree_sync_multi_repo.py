"""MR-3: dispatcher honors per-ticket repo binding for cwd + worktree lifecycle.

Extends the single-repo test_worktree_sync.py contract (which must keep passing
byte-identical, per that file's own tests) to N real repos: _worker_cwd resolves
a bound ticket's spawn cwd through its own repo, sync_worktrees fetches each
resolved repo once (dedup'd by realpath) against its own base_branch and
contains one repo's failure to that repo, and a merged ticket's worktree is
removed via its OWNING repo with a real removal failure surfaced instead of
silently swallowed. Every scenario drives real git repos (the conftest N-repo
factory) -- the only accepted mock is the VCS provider boundary (AC4), exactly
as tests/test_vcs_sync.py:54-57 already does.
"""
import json
import os
import stat
import subprocess

from maestro import cli, config as config_mod, dispatcher as disp, event_log, providers, \
    snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo


def _add_worktree(repo, home, key, branch, base="main"):
    wt = store.worktree_path(home, key)
    git("worktree", "add", "-q", "-b", branch, str(wt), base, cwd=repo)
    return wt


def _merge_new_commit(repo, origin, base="main", msg="merged change"):
    """Simulate another ticket's PR merging: a fresh commit lands on origin/<base>."""
    (repo / "NEWS.md").write_text(f"{msg}\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", msg, cwd=repo)
    git("push", "-q", "origin", base, cwd=repo)


def _repo_table(path, base_branch="main", default=False):
    return {"path": str(path), "slug": None, "base_branch": base_branch,
            "branch_prefix": "maestro/", "default": default}


def _write_config(home, *, repo_path=None, repos=None, vcs_sync_interval=None):
    """Write a real config.toml -- needed only by scenarios that drive `maestro`
    through the real CLI (`cli.main`), which always reloads Config from disk."""
    lines = ["[maestro]"]
    if repo_path is not None:
        lines.append(f'repo_path = "{repo_path}"')
    lines.append('branch_prefix = "maestro/"')
    for name, table in (repos or {}).items():
        lines.append(f"\n[repos.{name}]")
        lines.append(f'path = "{table["path"]}"')
        lines.append(f'base_branch = "{table["base_branch"]}"')
        if table.get("default"):
            lines.append("default = true")
    if vcs_sync_interval is not None:
        lines.append('\n[providers]')
        lines.append('vcs = "github_cli"')
        lines.append('\n[vcs.github_cli]')
        lines.append(f"sync_interval = {vcs_sync_interval}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_mod.load(str(home))


def _seed(home, key, phase, *, repo_name=None, pr=10):
    """Append events for one ticket bound (or not) to a [repos.<name>] entry,
    mimicking a real `create --repo` mint (frontmatter + TicketCreated.repo)."""
    front = f"repo: {repo_name}\n" if repo_name else ""
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n{front}")
    payload = {"title": key}
    if repo_name:
        payload["repo"] = repo_name
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    event_log.append(home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


class FakeVCS:
    """The only mock: the external GitHub boundary, per test_vcs_sync.py:54-57."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def pr_for_branch(self, branch):
        return None

    def pr_status(self, pr_number: int) -> dict:
        return self.statuses.get(pr_number, {
            "state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
            "ci_state": "unknown", "failing_checks": [],
        })

    def review_feedback(self, pr_number: int) -> list[dict]:
        return []


def _use_fake_vcs(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


# --- AC1: spawn cwd honors repo binding, unbound falls back to cfg.repo_path ---

def test_dispatch_spawn_cwd_is_owning_repo_for_bound_pre_worktree_ticket(home, tmp_path):
    _, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    _, default_repo = make_origin_and_repo(tmp_path, "default", base_branch="main")
    cfg = _write_config(home, repo_path=default_repo, repos={"beta": _repo_table(beta_repo)})

    assert cli.main(["--home", str(home), "create", "Unbound ticket",
                     "--key", "U-1", "--no-nudge"]) == 0
    assert cli.main(["--home", str(home), "create", "Beta ticket",
                     "--key", "B-1", "--repo", "beta", "--no-nudge"]) == 0

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)

    cwd_by_key = {k: c for k, _p, c, _m, _e in sessions.spawned}
    assert cwd_by_key["U-1"] == str(default_repo)   # unbound -> cfg.repo_path, unchanged
    assert cwd_by_key["B-1"] == str(beta_repo)       # bound -> its own repo, not the default


# --- AC2: two-repo sync_worktrees routes only the affected repo ---

def test_sync_worktrees_two_repo_routes_only_affected_repo(home, cfg, tmp_path):
    alpha_origin, alpha_repo = make_origin_and_repo(tmp_path, "alpha", base_branch="main")
    _beta_origin, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    cfg.repo_path = str(alpha_repo)
    cfg.repos = {"alpha": _repo_table(alpha_repo), "beta": _repo_table(beta_repo)}

    _seed(home, "A-1", Phase.IN_REVIEW, repo_name="alpha")
    _add_worktree(alpha_repo, home, "A-1", "maestro/A-1")
    _seed(home, "B-1", Phase.IN_REVIEW, repo_name="beta")
    _add_worktree(beta_repo, home, "B-1", "maestro/B-1")

    _merge_new_commit(alpha_repo, alpha_origin)  # only alpha's origin advances

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == ["A-1"]

    snap_a = snap_mod.load(home, "A-1")
    assert snap_a.phase == Phase.IMPLEMENTING.value
    changed = [e for e in event_log.read(home, "A-1") if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "origin/main" in changed[-1]["payload"].get("reason", "")

    assert snap_mod.load(home, "B-1").phase == Phase.IN_REVIEW.value  # untouched


def test_sync_worktrees_non_main_base_branch_fetched_and_ff_merged(home, cfg, tmp_path):
    dev_origin, dev_repo = make_origin_and_repo(tmp_path, "devrepo", base_branch="develop")
    # `default=True` makes this table double as the implicit default too (so a
    # human pointing cfg.repo_path at the same checkout without marking it
    # default would otherwise get a mismatched "main" base for that realpath --
    # see sync_worktrees' (realpath, base_branch) grouping key).
    cfg.repos = {"devrepo": _repo_table(dev_repo, base_branch="develop", default=True)}

    _seed(home, "D-1", Phase.AWAITING_CI, repo_name="devrepo")
    _add_worktree(dev_repo, home, "D-1", "maestro/D-1", base="develop")

    before = subprocess.run(["git", "-C", str(dev_repo), "rev-parse", "develop"],
                            capture_output=True, text=True, check=True).stdout.strip()
    _merge_new_commit(dev_repo, dev_origin, base="develop")

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == ["D-1"]
    changed = [e for e in event_log.read(home, "D-1") if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "origin/develop" in changed[-1]["payload"].get("reason", "")

    after = subprocess.run(["git", "-C", str(dev_repo), "rev-parse", "develop"],
                           capture_output=True, text=True, check=True).stdout.strip()
    origin_tip = subprocess.run(["git", "-C", str(dev_repo), "rev-parse", "origin/develop"],
                                capture_output=True, text=True, check=True).stdout.strip()
    assert before != after
    assert after == origin_tip


# --- AC3: fetch dedup across repos, keyed on realpath ---

def _install_git_argv_shim(tmp_path, monkeypatch, log_path):
    """Prepend a shim `git` onto PATH that logs argv then execs the real git --
    observation only, the loop under test still runs real git throughout."""
    import shutil
    real_git = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{log_path}"\nexec "{real_git}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")


def test_sync_worktrees_dedups_fetches_by_realpath(home, cfg, tmp_path, monkeypatch):
    _alpha_origin, alpha_repo = make_origin_and_repo(tmp_path, "alpha", base_branch="main")
    _beta_origin, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    alpha_alias = tmp_path / "alpha-alias"
    alpha_alias.symlink_to(alpha_repo)

    cfg.repo_path = str(alpha_repo)  # implicit default == alpha; must not add a 3rd fetch
    cfg.repos = {
        "alpha": _repo_table(alpha_repo),
        "alpha2": _repo_table(alpha_alias),   # realpath-resolves to alpha_repo
        "beta": _repo_table(beta_repo),
    }

    _seed(home, "A-1", Phase.AWAITING_CI, repo_name="alpha")
    _add_worktree(alpha_repo, home, "A-1", "maestro/A-1")
    _seed(home, "A-2", Phase.IN_REVIEW, repo_name="alpha2")
    _add_worktree(alpha_repo, home, "A-2", "maestro/A-2")
    _seed(home, "B-1", Phase.AWAITING_CI, repo_name="beta")
    _add_worktree(beta_repo, home, "B-1", "maestro/B-1")

    log_path = tmp_path / "git-argv.log"
    _install_git_argv_shim(tmp_path, monkeypatch, log_path)

    disp.sync_worktrees(cfg)

    lines = log_path.read_text().splitlines() if log_path.exists() else []
    fetch_calls = [l for l in lines if "fetch" in l.split()]
    assert len(fetch_calls) == 2, fetch_calls


# --- AC4: merged ticket's worktree is removed via its OWNING repo ---

def test_route_if_merged_removes_worktree_via_owning_repo(home, cfg, monkeypatch, tmp_path):
    _, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    cfg.repos = {"beta": _repo_table(beta_repo)}  # cfg.repo_path stays unset -- proves beta owns it
    fake = FakeVCS(statuses={77: {"state": "MERGED", "mergeable": "MERGEABLE",
                                    "head_sha": "sha1", "ci_state": "passing", "failing_checks": []}})
    _use_fake_vcs(cfg, monkeypatch, fake)

    _seed(home, "M-1", Phase.AWAITING_CI, repo_name="beta", pr=77)
    _add_worktree(beta_repo, home, "M-1", "maestro/M-1")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert not store.worktree_path(home, "M-1").exists()
    listing = subprocess.run(["git", "-C", str(beta_repo), "worktree", "list"],
                             capture_output=True, text=True, check=True).stdout
    assert "M-1" not in listing
    assert report.worktree_removal_errors == {}
    assert snap_mod.load(home, "M-1").phase == Phase.DONE.value


def test_forced_worktree_removal_failure_surfaces_in_report_and_cli_json(
        home, monkeypatch, tmp_path, capsys):
    _, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    _write_config(home, repos={"beta": _repo_table(beta_repo)}, vcs_sync_interval=0)
    fake = FakeVCS(statuses={88: {"state": "MERGED", "mergeable": "MERGEABLE",
                                    "head_sha": "sha1", "ci_state": "passing", "failing_checks": []}})
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)

    _seed(home, "M-2", Phase.AWAITING_CI, repo_name="beta", pr=88)
    # A plain directory git never registered via `worktree add` -- `worktree
    # remove --force` genuinely fails against it (a real git error, not a mock).
    wt = store.worktree_path(home, "M-2")
    wt.mkdir(parents=True)

    capsys.readouterr()
    rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)

    assert "M-2" in out["worktree_removal_errors"]
    assert out["worktree_removal_errors"]["M-2"]  # non-empty message, not vanished
    # The removal failure must not block the merge outcome itself.
    assert snap_mod.load(home, "M-2").phase == Phase.DONE.value


# --- AC5: one repo's fetch failure never stalls sibling sync or the sweep ---

def test_bad_repo_fetch_failure_contained_sweep_still_progresses(home, cfg, tmp_path):
    alpha_origin, alpha_repo = make_origin_and_repo(tmp_path, "alpha", base_branch="main")
    _beta_origin, beta_repo = make_origin_and_repo(tmp_path, "beta", base_branch="main")
    cfg.repo_path = str(alpha_repo)
    cfg.repos = {"alpha": _repo_table(alpha_repo), "beta": _repo_table(beta_repo)}

    _seed(home, "A-1", Phase.AWAITING_CI, repo_name="alpha")
    _add_worktree(alpha_repo, home, "A-1", "maestro/A-1")
    _seed(home, "B-1", Phase.AWAITING_CI, repo_name="beta")
    _add_worktree(beta_repo, home, "B-1", "maestro/B-1")

    _merge_new_commit(alpha_repo, alpha_origin)
    # beta's origin now points nowhere -- its fetch fails.
    git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=beta_repo)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "sync_worktrees" not in report.hook_errors  # never raised out of the hook
    assert snap_mod.load(home, "A-1").phase == Phase.IMPLEMENTING.value
    assert "A-1" in report.spawned
    assert snap_mod.load(home, "B-1").phase == Phase.AWAITING_CI.value  # untouched, not crashed
    # mint/backup/other ticks still ran this sweep (a stale heartbeat would be
    # the symptom of a sweep that aborted early).
    assert (home / "derived" / ".heartbeat.json").exists()


# --- AC6: no ad hoc home/worktrees/key construction remains in dispatcher.py ---

def test_no_ad_hoc_worktree_path_construction_remains():
    from pathlib import Path
    src = Path(disp.__file__).read_text(encoding="utf-8")
    assert '"worktrees"' not in src
    assert src.count("store.worktree_path(") >= 3
