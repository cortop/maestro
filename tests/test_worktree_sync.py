"""When one ticket's PR merges to origin/main, other tickets' worktrees drift
behind. sync_worktrees (run every dispatcher sweep) refreshes origin/main and
routes any awaiting-ci/in-review ticket whose worktree is now behind back into
implementing — the same auto-resolution path a GitHub-reported CONFLICTING PR
already uses — so the reconciler rebases on its next turn instead of
discovering the drift only when CI or a later mergeability check fails.
"""
import subprocess

from maestro import dispatcher as disp
from maestro import event_log, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


def _add_worktree(repo, home, key, branch, base="main"):
    wt = home / "worktrees" / key
    _git("worktree", "add", "-q", "-b", branch, str(wt), base, cwd=repo)
    return wt


def _merge_new_commit_to_origin(repo, origin, base="main"):
    """Simulate another ticket's PR merging: a fresh commit lands on origin/<base>."""
    (repo / "NEWS.md").write_text("merged change\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-9: merged change", cwd=repo)
    _git("push", "-q", "origin", base, cwd=repo)


def _merge_new_commit_to_origin_via_scratch_clone(tmp_path, origin, base="main"):
    """Like `_merge_new_commit_to_origin`, but lands the commit on `origin/<base>` via a
    throwaway clone rather than committing in `repo`'s own working tree -- for tests where
    `repo` has some other branch checked out and must stay untouched by the act of
    advancing origin."""
    scratch = tmp_path / "scratch"
    _git("clone", "-q", str(origin), str(scratch), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=scratch)
    _git("config", "user.name", "Test", cwd=scratch)
    (scratch / "NEWS.md").write_text("merged change\n")
    _git("add", "-A", cwd=scratch)
    _git("commit", "-q", "-m", "T-9: merged change", cwd=scratch)
    _git("push", "-q", "origin", base, cwd=scratch)


def _seed(home, key, phase, pr=10):
    store.atomic_write(
        store.spec_path(home, key),
        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n",
    )
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def _ci_passing(home, key):
    """T-82: a drift-only reroute now requires a POSITIVELY known-safe CI state
    (never None/unobserved, never "unknown") -- these tests are about the
    drift/routing mechanism itself, not the CI gate, so give them an observed
    passing CI up front."""
    event_log.append(home, key, "CiObserved",
                     {"state": "passing", "failing_checks": []}, actor="r")
    return snap_mod.rebuild(home, key)


def test_sync_worktrees_routes_stale_awaiting_ci_ticket_to_implementing(home, cfg, tmp_path):
    # MTO-2: "always" opts back into today's unconditional-on-drift behavior --
    # the new default (on_conflict) never routes on drift alone; see
    # test_drift_policy.py for default/on_conflict/daily coverage.
    cfg.base_drift_policy = "always"
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.AWAITING_CI)
    _ci_passing(home, "T-5")
    _add_worktree(repo, home, "T-5", "maestro/T-5")

    _merge_new_commit_to_origin(repo, origin)  # another ticket's PR just landed on main

    result = disp.sync_worktrees(cfg)
    assert result["fetched"] is True
    assert result["routed"] == ["T-5"]

    snap = snap_mod.load(home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    changed = [e for e in event_log.read(home, "T-5") if e["type"] == "PhaseChanged"
               and e["payload"].get("phase") == Phase.IMPLEMENTING.value]
    assert changed and "origin/main" in changed[-1]["payload"].get("reason", "")


def test_sync_worktrees_is_a_noop_when_worktree_already_current(home, cfg, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.IN_REVIEW)
    _add_worktree(repo, home, "T-5", "maestro/T-5")
    # No new commits land on origin/main — nothing to sync.

    result = disp.sync_worktrees(cfg)
    assert result["fetched"] is True
    assert result["routed"] == []
    assert snap_mod.load(home, "T-5").phase == Phase.IN_REVIEW.value


def test_sync_worktrees_skips_ticket_already_implementing(home, cfg, tmp_path):
    """implementing already re-syncs with origin/main on its own every turn — no nudge needed."""
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.IMPLEMENTING)
    _add_worktree(repo, home, "T-5", "maestro/T-5")
    _merge_new_commit_to_origin(repo, origin)

    result = disp.sync_worktrees(cfg)
    assert result["routed"] == []
    assert snap_mod.load(home, "T-5").phase == Phase.IMPLEMENTING.value


def test_sync_worktrees_updates_local_main_when_checked_out(home, cfg, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    before = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                            capture_output=True, text=True, check=True).stdout.strip()

    _merge_new_commit_to_origin(repo, origin)

    disp.sync_worktrees(cfg)
    after = subprocess.run(["git", "rev-parse", "main"], cwd=repo,
                           capture_output=True, text=True, check=True).stdout.strip()
    origin_tip = subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()
    assert before != after
    assert after == origin_tip


def test_sync_worktrees_leaves_feature_branch_ancestor_of_origin_main_untouched(home, cfg, tmp_path):
    """QW-6: a human feature branch that is an ancestor of origin/main (freshly branched,
    or fully merged) must not be silently fast-forwarded -- `--ff-only` operates on
    whatever HEAD points at, not just `base`."""
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    _git("checkout", "-q", "-b", "feature/human-branch", cwd=repo)
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                 capture_output=True, text=True, check=True).stdout.strip()
    before_ref = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()

    # origin/main advances; feature branch is behind it, but stays checked out in `repo`
    _merge_new_commit_to_origin_via_scratch_clone(tmp_path, origin)

    disp.sync_worktrees(cfg)

    after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()
    after_ref = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo,
                               capture_output=True, text=True, check=True).stdout.strip()
    assert after_head == before_head
    assert after_ref == before_ref == "feature/human-branch"


def test_sync_worktrees_noop_on_detached_head(home, cfg, tmp_path):
    """A detached HEAD (e.g. mid-rebase, or a manual checkout of a sha) has no branch
    for `symbolic-ref` to name -- the merge must be skipped, and nothing should raise."""
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    _git("checkout", "-q", head_sha, cwd=repo)  # detach

    _merge_new_commit_to_origin_via_scratch_clone(tmp_path, origin)

    result = disp.sync_worktrees(cfg)  # must not raise

    after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout.strip()
    assert after_head == head_sha  # unchanged: no merge happened
    assert result["errors"] == {}


def test_dispatch_full_sweep_routes_and_spawns_stale_ticket(home, cfg, tmp_path):
    """End-to-end: a real dispatcher sweep (not just sync_worktrees directly) both
    routes the stale ticket to implementing and spawns a reconciler for it in the
    same pass, since implementing is an active (non-sleeping) phase."""
    cfg.base_drift_policy = "always"  # MTO-2: new default (on_conflict) never routes on drift alone
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.AWAITING_CI)
    _ci_passing(home, "T-5")
    _add_worktree(repo, home, "T-5", "maestro/T-5")
    _merge_new_commit_to_origin(repo, origin)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-5" in report.spawned
    assert snap_mod.load(home, "T-5").phase == Phase.IMPLEMENTING.value
