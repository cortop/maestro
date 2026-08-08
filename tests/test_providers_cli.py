"""MR-4: GitHubCliVCS's `--repo <slug>` argv construction — the VCS Protocol's
optional `repo` keyword is passed-repo-first, with the `self.repos[0]`/iterate-all
behavior as the `repo=None` fallback so single-repo boards stay byte-identical to
before. The only mock is `_run` (the `gh` subprocess boundary itself).

GA-6: `classify_gh_failure` maps a failed `gh` invocation's stderr text to
auth/not_found/transient/unknown, pinned against the verbatim gh 2.94.0 stderr
strings this ticket measured — never keying on exit code alone, since real gh
exits 1 for all of them. `_run` itself distinguishes a missing `gh` executable
(FileNotFoundError) from a hung one (subprocess.TimeoutExpired) with no network
call (subprocess.run is monkeypatched to raise, not actually invoked)."""
import subprocess

import pytest

from maestro.providers import cli as cli_mod
from maestro.providers.base import NullVCS
from maestro.providers.cli import GitHubCliVCS, classify_gh_failure


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


def test_null_vcs_pr_status_has_no_error_key():
    """base.py:77-79 -- NullVCS.pr_status is a valid *no-error* dict."""
    assert "error" not in NullVCS().pr_status(1)


# --- GA-6: classify_gh_failure -- pinned against real gh 2.94.0 stderr ----------

@pytest.mark.parametrize("stderr", [
    "HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
    "Try authenticating with:  gh auth login -h github.com",
    "GraphQL: Resource protected by organization SAML enforcement. You must "
    "grant your OAuth token access to this organization. (repository)",
])
def test_classify_auth_markers(stderr):
    assert classify_gh_failure(1, "", stderr) == "auth"


@pytest.mark.parametrize("stderr", [
    "GraphQL: Could not resolve to a Repository with the name 'owner/repo'. (repository)",
    "GraphQL: Could not resolve to a PullRequest with the number of 999999. "
    "(repository.pullRequest)",
])
def test_classify_not_found_markers(stderr):
    assert classify_gh_failure(1, "", stderr) == "not_found"


def test_classify_timeout_is_transient():
    # The exact text _run's TimeoutExpired branch produces (see below).
    stderr = "maestro: command timed out after 60s: Command '['gh']' timed out after 60 seconds"
    assert classify_gh_failure(124, "", stderr) == "transient"


def test_classify_unrecognized_stderr_is_unknown():
    """Unmatched text degrades to today's behavior, never a guess or a dead-letter."""
    assert classify_gh_failure(1, "", "gh: some future error format we've never seen") == "unknown"


def test_classify_never_keys_on_exit_code_alone():
    """Real gh exits 1 for auth, not-found, AND generic failures alike -- the
    same rc must classify differently purely from stderr text."""
    assert classify_gh_failure(1, "", "HTTP 401: Bad credentials") == "auth"
    assert classify_gh_failure(1, "", "Could not resolve to a Repository with the name 'x'.") == "not_found"
    assert classify_gh_failure(1, "", "some unrelated error") == "unknown"


def test_classify_saml_enforcement_wins_over_accidental_not_found_reading():
    """The SAML message is textually adjacent to a 404 (a human might call it
    "not found") but it's actually an auth problem -- must classify as auth."""
    stderr = ("GraphQL: Resource protected by organization SAML enforcement. "
              "You must grant your OAuth token access to this organization. (repository)")
    assert "not found" not in stderr.lower()  # sanity: no accidental collision either
    assert classify_gh_failure(1, "", stderr) == "auth"


# --- GA-6: _run distinguishes FileNotFoundError vs TimeoutExpired --------------

def test_run_file_not_found_is_not_transient(monkeypatch):
    def raise_fnf(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")
    monkeypatch.setattr(subprocess, "run", raise_fnf)

    rc, out, err = cli_mod._run(["gh", "pr", "view", "1"])
    assert out == ""
    assert classify_gh_failure(rc, out, err) != "transient"
    assert classify_gh_failure(rc, out, err) == "auth"


def test_run_timeout_expired_is_transient(monkeypatch):
    def raise_timeout(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
    monkeypatch.setattr(subprocess, "run", raise_timeout)

    rc, out, err = cli_mod._run(["gh", "pr", "view", "1"], timeout=60)
    assert out == ""
    assert classify_gh_failure(rc, out, err) == "transient"


def test_run_distinguishes_the_two_exceptions_from_each_other(monkeypatch):
    """AC 2's core claim: same generic `except` today collapses both to
    `1, "", str(e)`; afterwards they must classify to different classes."""
    def raise_fnf(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(subprocess, "run", raise_fnf)
    fnf_class = classify_gh_failure(*cli_mod._run(["gh"]))

    def raise_timeout(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
    monkeypatch.setattr(subprocess, "run", raise_timeout)
    timeout_class = classify_gh_failure(*cli_mod._run(["gh"]))

    assert fnf_class != timeout_class


# --- GA-6: pr_status carries the classification on a NEW field -----------------

def test_pr_status_failure_carries_error_field(monkeypatch):
    def fake_run(cmd, timeout=60):
        return 1, "", "HTTP 401: Bad credentials (https://api.github.com/graphql)"
    monkeypatch.setattr(cli_mod, "_run", fake_run)

    status = GitHubCliVCS({}).pr_status(7)
    assert status["error"] == "auth"
    assert status["ci_state"] == "unknown"  # the 4-value vocabulary is untouched


def test_pr_status_success_has_no_error_key(monkeypatch):
    """Byte-identical to today on the success path -- no new key leaks in."""
    calls = _stub_run(monkeypatch)
    status = GitHubCliVCS({}).pr_status(7)
    assert "error" not in status
