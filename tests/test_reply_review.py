"""T-128: `maestro reply-review` -- reply to a PR review comment IN ITS OWN
THREAD via the VCS provider (an `inline-<id>` comment threads through the
`/replies` endpoint; any other id gets one quoting PR comment instead), never
an improvised new top-level comment. Idempotent per (comment_id, tree_sha);
refuses an over-length or jargon-carrying body.

Real-surface QA (CLAUDE.md): the CLI-level test stubs the external `gh`
boundary itself (a real executable on PATH, the pattern `tests/test_health.py`
uses for `launchctl`) to prove the real `/replies` URL is actually called --
never the Python VCS layer, which IS the component under test there. The
ops-level tests use a FakeVCS (the sanctioned mock boundary everywhere else in
this suite, e.g. `tests/test_ci_rerun.py`) to cover validation/idempotency/
quoting without a live `gh`.
"""
from __future__ import annotations

import os

import pytest

from maestro import ops, providers, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.config import Config

from conftest import git, make_origin_and_repo, seed_ticket


class FakeVCS:
    """The only mock at the ops layer: the external GitHub boundary."""

    def __init__(self, reviews=None):
        self.reviews = reviews or []
        self.reply_calls: list[tuple] = []
        self.comment_calls: list[tuple] = []

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
                "ci_state": "unknown", "failing_checks": []}

    def review_feedback(self, pr_number, repo=None, env=None):
        return self.reviews

    def pr_ready(self, pr_number, repo=None, env=None):
        return {"ok": False, "error": "unknown"}

    def rerun_failed(self, pr_number, head_sha, repo=None, env=None):
        return {"ok": False}

    def failed_log_tail(self, run_id, max_bytes=2048, repo=None, env=None):
        return ""

    def reply_to_review_comment(self, pr_number, comment_id, body, repo=None, env=None):
        self.reply_calls.append((pr_number, comment_id, body, repo))
        return {"ok": True}

    def comment_pr(self, pr_number, body, repo=None, env=None):
        self.comment_calls.append((pr_number, body, repo))
        return {"ok": True}


def _use_fake(cfg, monkeypatch, fake):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _seed_with_worktree(home, key="T-1", pr=42):
    _, repo = make_origin_and_repo(home / "worktrees", name=key)
    seed_ticket(home, key, "reply review", phase="implementing", pr=pr)
    return repo


# ---------------------------------------------------------------------------
# AC4: length + jargon gate, enforced by the verb itself
# ---------------------------------------------------------------------------

def test_reply_review_refuses_body_over_length_limit(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home)
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError, match="600"):
        ops.reply_review(cfg, "T-1", "inline-1", "x" * 601)


def test_reply_review_refuses_empty_body(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home)
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError):
        ops.reply_review(cfg, "T-1", "inline-1", "   ")


def test_reply_review_refuses_raw_content_hash(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home)
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError, match="content-hash"):
        ops.reply_review(cfg, "T-1", "inline-1", "Fixed, matches 0123456789abcdef exactly.")


@pytest.mark.parametrize("phrase", ["step_id", "step-id", "step id", "AC-hash", "ac_hash"])
def test_reply_review_refuses_literal_jargon_phrases(home, monkeypatch, phrase):
    cfg = Config(home=home)
    _seed_with_worktree(home)
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError, match="jargon"):
        ops.reply_review(cfg, "T-1", "inline-1", f"Fixed, see {phrase} for detail.")


@pytest.mark.parametrize("phase_name", ["awaiting-ci", "awaiting-human", "in-review"])
def test_reply_review_refuses_hyphenated_phase_names(home, monkeypatch, phase_name):
    cfg = Config(home=home)
    _seed_with_worktree(home)
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError, match="phase name"):
        ops.reply_review(cfg, "T-1", "inline-1", f"Moved to {phase_name} now.")


def test_reply_review_allows_common_single_word_phase_names(home, monkeypatch):
    """Single-word phase names ('ready', 'done', 'qa', ...) double as ordinary
    English a genuine reviewer-facing reply needs to say -- only the
    hyphenated ones are unambiguous jargon (ops._JARGON_PHASE_NAMES)."""
    cfg = Config(home=home)
    _seed_with_worktree(home)
    fake = FakeVCS()
    _use_fake(cfg, monkeypatch, fake)
    result = ops.reply_review(cfg, "T-1", "inline-1", "Fixed and ready for another look.")
    assert result["posted"] is True


# ---------------------------------------------------------------------------
# AC1/AC3: inline reply threads via /replies, idempotent per (comment, tree)
# ---------------------------------------------------------------------------

def test_reply_review_inline_posts_via_replies_endpoint_and_is_idempotent(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home, pr=42)
    fake = FakeVCS()
    _use_fake(cfg, monkeypatch, fake)

    body = "Fixed the race; see the latest commit."
    result = ops.reply_review(cfg, "T-1", "inline-555", body)
    assert result["posted"] is True
    assert result["kind"] == "inline"
    assert fake.reply_calls == [(42, "555", body, "cortop/maestro")]
    assert not fake.comment_calls

    snap = snap_mod.load(home, "T-1")
    assert result["tree_sha"] in snap.review_replies["inline-555"]

    # Re-run at the SAME tree state: idempotent no-op, no second `gh` call.
    result2 = ops.reply_review(cfg, "T-1", "inline-555", body)
    assert result2["posted"] is False
    assert len(fake.reply_calls) == 1


def test_reply_review_posts_again_after_a_further_commit(home, monkeypatch):
    cfg = Config(home=home)
    repo = _seed_with_worktree(home, pr=42)
    fake = FakeVCS()
    _use_fake(cfg, monkeypatch, fake)

    ops.reply_review(cfg, "T-1", "inline-555", "Fixed the race.")
    (repo / "x.txt").write_text("more\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "more", cwd=repo)

    result = ops.reply_review(cfg, "T-1", "inline-555", "Fixed the race.")
    assert result["posted"] is True
    assert len(fake.reply_calls) == 2


# ---------------------------------------------------------------------------
# AC2: a non-inline (review-body) id gets one quoting PR comment with a link
# ---------------------------------------------------------------------------

def test_reply_review_non_inline_quotes_original_and_links(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home, pr=42)
    fake = FakeVCS(reviews=[{"id": "999", "state": "CHANGES_REQUESTED",
                            "body": "Please rename this variable.\nMore context.",
                            "author": "octocat"}])
    _use_fake(cfg, monkeypatch, fake)

    result = ops.reply_review(cfg, "T-1", "999", "Renamed it in the latest commit.")
    assert result["kind"] == "review"
    assert len(fake.comment_calls) == 1
    pr_number, posted_body, repo_slug = fake.comment_calls[0]
    assert pr_number == 42
    assert repo_slug == "cortop/maestro"
    assert posted_body.startswith("> Please rename this variable.")
    assert "pull/42#pullrequestreview-999" in posted_body
    assert posted_body.endswith("Renamed it in the latest commit.")


# ---------------------------------------------------------------------------
# Misuse guards
# ---------------------------------------------------------------------------

def test_reply_review_refuses_without_open_pr(home, monkeypatch):
    cfg = Config(home=home)
    make_origin_and_repo(home / "worktrees", name="T-1")
    seed_ticket(home, "T-1", "no pr", phase="implementing")
    _use_fake(cfg, monkeypatch, FakeVCS())
    with pytest.raises(store.MaestroError, match="no open PR"):
        ops.reply_review(cfg, "T-1", "inline-1", "hello there")


def test_reply_review_raises_when_vcs_post_fails(home, monkeypatch):
    cfg = Config(home=home)
    _seed_with_worktree(home)

    class FailingVCS(FakeVCS):
        def reply_to_review_comment(self, pr_number, comment_id, body, repo=None, env=None):
            return {"ok": False, "error": "transient"}

    _use_fake(cfg, monkeypatch, FailingVCS())
    with pytest.raises(store.MaestroError, match="failed to post"):
        ops.reply_review(cfg, "T-1", "inline-1", "hello there")


# ---------------------------------------------------------------------------
# AC6: a CLI test over a temp home with a stubbed `gh` -- the replies URL is
# hit for an inline id, and a second identical call is a no-op.
# ---------------------------------------------------------------------------

def test_reply_review_cli_hits_real_replies_endpoint_and_is_idempotent(home, monkeypatch, capsys):
    key = "T-1"
    _seed_with_worktree(home, key=key, pr=7)

    bin_dir = home.parent / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    gh_log = home.parent / "gh.log"
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(f'#!/bin/sh\necho "$@" >> "{gh_log}"\necho "{{}}"\nexit 0\n')
    gh_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    (home / "config.toml").write_text(
        '[maestro]\n\n[providers]\nvcs = "github_cli"\n', encoding="utf-8")

    body = "Fixed the typo in the latest commit."
    rc = cli_main(["--home", str(home), "reply-review", key,
                   "--comment-id", "inline-321", "--body", body])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"posted": true' in out

    lines = [l for l in gh_log.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert "repos/cortop/maestro/pulls/7/comments/321/replies" in lines[0]
    assert f"body={body}" in lines[0]

    rc2 = cli_main(["--home", str(home), "reply-review", key,
                    "--comment-id", "inline-321", "--body", body])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert '"posted": false' in out2

    lines_after = [l for l in gh_log.read_text().splitlines() if l.strip()]
    assert len(lines_after) == 1  # unchanged -- no second `gh` call
