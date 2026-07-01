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


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_origin_and_repo(tmp_path):
    """A bare origin plus a clone (standing in for the primary repo maestro builds in)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=origin)

    repo = tmp_path / "repo"
    _git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    return origin, repo


def _add_worktree(repo, home, key, branch):
    wt = home / "worktrees" / key
    _git("worktree", "add", "-q", "-b", branch, str(wt), "main", cwd=repo)
    return wt


def _merge_new_commit_to_origin(repo, origin):
    """Simulate another ticket's PR merging: a fresh commit lands on origin/main."""
    (repo / "NEWS.md").write_text("merged change\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-9: merged change", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)


def _seed(home, key, phase, pr=10):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def test_sync_worktrees_routes_stale_awaiting_ci_ticket_to_implementing(home, cfg, tmp_path):
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.AWAITING_CI)
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


def test_dispatch_full_sweep_routes_and_spawns_stale_ticket(home, cfg, tmp_path):
    """End-to-end: a real dispatcher sweep (not just sync_worktrees directly) both
    routes the stale ticket to implementing and spawns a reconciler for it in the
    same pass, since implementing is an active (non-sleeping) phase."""
    origin, repo = _make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)

    _seed(home, "T-5", Phase.AWAITING_CI)
    _add_worktree(repo, home, "T-5", "maestro/T-5")
    _merge_new_commit_to_origin(repo, origin)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "T-5" in report.spawned
    assert snap_mod.load(home, "T-5").phase == Phase.IMPLEMENTING.value
