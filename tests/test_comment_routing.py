"""T-147: comment-only inline reviews and Conversation-tab PR comments route the
ticket back to implementing, and maestro never routes on its own replies. The
only mocks are the GitHub boundary (a fake VCS / a stubbed `_run`)."""
import json

from maestro import dispatcher as disp, event_log, ops, providers, \
    snapshot as snap_mod, store
from maestro.providers import cli as cli_mod
from maestro.providers.cli import GitHubCliVCS
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import make_origin_and_repo, seed_ticket


class FakeVCS:
    def __init__(self, reviews=None):
        self.reviews = list(reviews or [])
        self._next = 900

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
                "ci_state": "passing", "failing_checks": []}

    def review_feedback(self, pr_number, repo=None, env=None):
        return list(self.reviews)

    def reply_to_review_comment(self, pr_number, comment_id, body, repo=None, env=None):
        self._next += 1
        rid = f"inline-{self._next}"
        self.reviews.append({"id": rid, "state": "INLINE_COMMENT", "body": body,
                             "path": "a.py", "line": 1, "author": "me"})
        return {"ok": True, "id": rid}

    def comment_pr(self, pr_number, body, repo=None, env=None):
        self._next += 1
        rid = f"issue-{self._next}"
        self.reviews.append({"id": rid, "state": "ISSUE_COMMENT", "body": body, "author": "me"})
        return {"ok": True, "id": rid}


def _seed(cfg, key="T-5", phase=Phase.IN_REVIEW, pr=42):
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _use_fake(cfg, monkeypatch, fake):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _impl_reasons(home, key):
    return [e["payload"].get("reason", "") for e in event_log.read(home, key)
            if e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value]


def _inline(cid, body, path="a.py", line=3):
    return {"id": f"inline-{cid}", "state": "INLINE_COMMENT", "body": body,
            "path": path, "line": line, "author": "rev"}


def _issue(cid, body, author="rev"):
    return {"id": f"issue-{cid}", "state": "ISSUE_COMMENT", "body": body, "author": author}


def test_inline_comments_alone_route_to_implementing(cfg, monkeypatch):
    fake = FakeVCS([_inline(1, "rename this"), _inline(2, "add a test", "b.py", 9)])
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
    reason = _impl_reasons(cfg.home, "T-5")[-1]
    assert "a.py:3: rename this" in reason and "b.py:9: add a test" in reason
    assert reason.startswith("review comment:")


def test_conversation_comment_routes_and_noise_suppresses(cfg, monkeypatch):
    _use_fake(cfg, monkeypatch, FakeVCS([_issue(7, "please also handle empty input")]))
    _seed(cfg)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert _impl_reasons(cfg.home, "T-5")[-1] == "review comment: please also handle empty input"


def test_conversation_comment_noise_author_does_not_route(cfg, monkeypatch):
    cfg.review_noise_authors = ["ci-bot"]
    _use_fake(cfg, monkeypatch, FakeVCS([_issue(7, "coverage 91%", author="ci-bot")]))
    _seed(cfg)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value


def test_conversation_comment_noise_pattern_does_not_route(cfg, monkeypatch):
    cfg.review_noise_patterns = [r"LGTM!?"]
    _use_fake(cfg, monkeypatch, FakeVCS([_issue(7, "LGTM")]))
    _seed(cfg)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value


def test_reobserving_same_comment_ids_never_routes_twice(cfg, monkeypatch):
    fake = FakeVCS([_inline(1, "rename this"), _issue(2, "and this")])
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg)
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert len(_impl_reasons(cfg.home, "T-5")) == 1
    # Back to in-review (as after a fix round); same ids still returned by GitHub.
    event_log.append(cfg.home, "T-5", "PhaseChanged", {"phase": "in-review"}, actor="r")
    snap_mod.rebuild(cfg.home, "T-5")
    disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert len(_impl_reasons(cfg.home, "T-5")) == 1
    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value


def test_sweep_after_reply_review_does_not_bounce(cfg, monkeypatch):
    key = "T-1"
    _, _repo = make_origin_and_repo(cfg.home / "worktrees", name=key)
    seed_ticket(cfg.home, key, "reply", phase="implementing", pr=42)
    fake = FakeVCS([_inline(5, "fix this")])
    _use_fake(cfg, monkeypatch, fake)
    ops.reply_review(cfg, key, "inline-5", "Fixed in the latest commit.")
    ev = [e["payload"] for e in event_log.read(cfg.home, key) if e["type"] == "ReviewReplyPosted"]
    assert [p["posted_id"] for p in ev] == ["inline-901"]

    event_log.append(cfg.home, key, "PhaseChanged", {"phase": "in-review"}, actor="r")
    snap_mod.rebuild(cfg.home, key)
    # Only the replies are new; the human comments were never observed before.
    fake.reviews = [r for r in fake.reviews if r["id"] == "inline-901"]
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(cfg.home, key).phase == Phase.IN_REVIEW.value


def _stub_gh(monkeypatch, by_endpoint):
    def fake_run(cmd, timeout=60, env=None):
        for frag, result in by_endpoint.items():
            if any(frag in c for c in cmd):
                return result
        return 0, "[]", ""
    monkeypatch.setattr(cli_mod, "_run", fake_run)


def test_review_feedback_returns_issue_comments_with_own_prefix(monkeypatch):
    payload = [{"id": 11, "body": "hello", "user": {"login": "u"}},
               {"id": 12, "body": "  ", "user": {"login": "u"}}]
    _stub_gh(monkeypatch, {"issues/7/comments": (0, json.dumps(payload), ""),
                           "pr": (0, '{"reviews": []}', "")})
    out = GitHubCliVCS({}).review_feedback(7, repo="o/r")
    assert out == [{"id": "issue-11", "state": "ISSUE_COMMENT", "body": "hello", "author": "u"}]


def test_review_feedback_issue_comments_failed_or_garbled_yield_none(monkeypatch):
    for res in [(1, "", "boom"), (0, "not json", ""), (0, "{}", "")]:
        _stub_gh(monkeypatch, {"issues/7/comments": res, "pr": (0, '{"reviews": []}', "")})
        assert GitHubCliVCS({}).review_feedback(7, repo="o/r") == []


def test_reply_calls_return_posted_comment_id(monkeypatch):
    _stub_gh(monkeypatch, {"replies": (0, '{"id": 77}', ""),
                           "comment": (0, "https://github.com/o/r/pull/7#issuecomment-88\n", "")})
    vcs = GitHubCliVCS({})
    assert vcs.reply_to_review_comment(7, "5", "b", repo="o/r") == {"ok": True, "id": "inline-77"}
    assert vcs.comment_pr(7, "b", repo="o/r") == {"ok": True, "id": "issue-88"}
