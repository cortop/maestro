"""A conflicting PR routes back to implementing without a human nudge, even when
GitHub never settles `mergeable` (a fast-moving base keeps it `UNKNOWN`):
`sync_worktrees` tests the real merge locally with `git merge-tree`, and
`sync_vcs` checks every unmerged entry of a PR stack, not just the tracked one.
Real git repos throughout; only the VCS provider boundary is faked.
"""
from maestro import dispatcher as disp, event_log, providers, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import add_worktree, git, make_origin_and_repo


class _FakeVCS:
    """GitHub never settles mergeability: every PR polls as UNKNOWN unless listed."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return self.statuses.get(pr_number, {
            "state": "OPEN", "mergeable": "UNKNOWN", "head_sha": "sha1",
            "ci_state": "pending", "failing_checks": [],
        })

    def review_feedback(self, pr_number, repo=None, env=None):
        return []


def _use_fake(cfg, monkeypatch, fake):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _seed(home, key, phase, *, prs=(42,)):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated", {"title": key}, actor="d")
    for i, pr in enumerate(prs):
        payload = {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False}
        if len(prs) > 1:
            base = "main" if i == 0 else f"maestro/{key}-{i}"
            payload["stack"] = {"index": i, "total": len(prs),
                                "branch": f"maestro/{key}-{i + 1}", "base": base}
        event_log.append(home, key, "PrOpened", payload, actor="r")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(home, key)


def _commit(cwd, path, text, msg):
    (cwd / path).write_text(text)
    git("add", "-A", cwd=cwd)
    git("commit", "-q", "-m", msg, cwd=cwd)


def _implementing_reasons(home, key):
    return [e["payload"].get("reason", "") for e in event_log.read(home, key)
            if e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value]


def test_base_conflict_routes_to_implementing_while_github_reports_unknown(
        home, cfg, tmp_path, monkeypatch):
    assert cfg.base_drift_policy == "on_conflict"
    _use_fake(cfg, monkeypatch, _FakeVCS())
    origin, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    _seed(home, "T-9", Phase.AWAITING_CI)
    wt = add_worktree(repo, home, "T-9", "maestro/T-9")
    _commit(wt, "README.md", "ticket edit\n", "ticket change")
    _commit(repo, "README.md", "main edit\n", "another PR lands")
    git("push", "-q", "origin", "main", cwd=repo)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert snap_mod.load(home, "T-9").phase == Phase.IMPLEMENTING.value
    assert _implementing_reasons(home, "T-9") == ["resolve merge conflict for PR #42"]
    assert "T-9" in report.spawned
    assert report.drift_skipped == []


def test_clean_drift_is_not_routed_under_on_conflict(home, cfg, tmp_path):
    origin, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    _seed(home, "T-9", Phase.AWAITING_CI)
    wt = add_worktree(repo, home, "T-9", "maestro/T-9")
    _commit(wt, "FEATURE.md", "ticket edit\n", "ticket change")
    _commit(repo, "NEWS.md", "main edit\n", "another PR lands")
    git("push", "-q", "origin", "main", cwd=repo)

    result = disp.sync_worktrees(cfg)

    assert result["conflicted"] == []
    assert result["skipped_by_policy"] == ["T-9"]
    assert snap_mod.load(home, "T-9").phase == Phase.AWAITING_CI.value


def test_conflicting_non_tracked_stack_entry_routes_to_implementing(cfg, monkeypatch):
    _use_fake(cfg, monkeypatch, _FakeVCS({101: {
        "state": "OPEN", "mergeable": "CONFLICTING", "head_sha": "sha101",
        "ci_state": "unknown", "failing_checks": [],
    }}))
    _seed(cfg.home, "T-5", Phase.IN_REVIEW, prs=(100, 101, 102))

    disp.sync_vcs(cfg, now=1000)

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert snap.pr_number == 100
    assert _implementing_reasons(cfg.home, "T-5") == ["resolve merge conflict for PR #101"]
