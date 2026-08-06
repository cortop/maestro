"""MR-4: GitHubCliVCS's `--repo <slug>` argv construction — the VCS Protocol's
optional `repo` keyword is passed-repo-first, with the `self.repos[0]`/iterate-all
behavior as the `repo=None` fallback so single-repo boards stay byte-identical to
before. The only mock is `_run` (the `gh` subprocess boundary itself)."""
from maestro.providers import cli as cli_mod
from maestro.providers.base import NullVCS
from maestro.providers.cli import GitHubCliVCS


def _stub_run(monkeypatch, out="{}"):
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return 0, out, ""

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    return calls


def test_pr_status_passes_explicit_repo_regardless_of_repos_ordering(monkeypatch):
    calls = _stub_run(monkeypatch)
    vcs = GitHubCliVCS({"repos": ["owner/other1", "owner/other2"]})
    vcs.pr_status(7, repo="acme/beta")
    assert calls == [["gh", "pr", "view", "7", "--json",
                      "state,mergeable,headRefOid,statusCheckRollup", "--repo", "acme/beta"]]


def test_pr_status_repo_none_is_byte_identical_to_todays_repos0_behavior(monkeypatch):
    calls = _stub_run(monkeypatch)
    vcs = GitHubCliVCS({"repos": ["owner/first", "owner/second"]})
    vcs.pr_status(7)
    assert calls == [["gh", "pr", "view", "7", "--json",
                      "state,mergeable,headRefOid,statusCheckRollup", "--repo", "owner/first"]]


def test_pr_status_repo_none_no_repos_configured_omits_the_flag(monkeypatch):
    calls = _stub_run(monkeypatch)
    vcs = GitHubCliVCS({})
    vcs.pr_status(7)
    assert calls == [["gh", "pr", "view", "7", "--json",
                      "state,mergeable,headRefOid,statusCheckRollup"]]


def test_review_feedback_passes_explicit_repo_regardless_of_repos_ordering(monkeypatch):
    calls = _stub_run(monkeypatch)
    vcs = GitHubCliVCS({"repos": ["owner/other1", "owner/other2"]})
    vcs.review_feedback(9, repo="acme/beta")
    assert calls == [["gh", "pr", "view", "9", "--json", "reviews", "--repo", "acme/beta"]]


def test_review_feedback_repo_none_is_byte_identical_to_todays_repos0_behavior(monkeypatch):
    calls = _stub_run(monkeypatch)
    vcs = GitHubCliVCS({"repos": ["owner/first"]})
    vcs.review_feedback(9)
    assert calls == [["gh", "pr", "view", "9", "--json", "reviews", "--repo", "owner/first"]]


def test_pr_for_branch_with_repo_queries_only_that_repo(monkeypatch):
    calls = _stub_run(monkeypatch, out="[]")
    vcs = GitHubCliVCS({"repos": ["owner/a", "owner/b"]})
    vcs.pr_for_branch("maestro/T-1", repo="acme/beta")
    assert len(calls) == 1
    assert calls[0][-2:] == ["--repo", "acme/beta"]


def test_pr_for_branch_repo_none_iterates_all_repos_as_before(monkeypatch):
    calls = _stub_run(monkeypatch, out="[]")
    vcs = GitHubCliVCS({"repos": ["owner/a", "owner/b"]})
    vcs.pr_for_branch("maestro/T-1")
    assert [c[-2:] for c in calls] == [["--repo", "owner/a"], ["--repo", "owner/b"]]


def test_null_vcs_accepts_repo_keyword_without_typeerror():
    """Protocol conformance: any non-'none' vcs name falling through the provider
    registry to NullVCS must not TypeError under the new repo= call shape."""
    vcs = NullVCS()
    assert vcs.pr_for_branch("branch", repo="acme/beta") is None
    status = vcs.pr_status(1, repo="acme/beta")
    assert status["state"] == "unknown"
    assert vcs.review_feedback(1, repo="acme/beta") == []
