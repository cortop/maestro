"""T-296: a moved PR head is noticed once, `reviews rereview` / `reviews retry` give the way forward.

Real CLI, real git worktrees, real dispatcher sweeps over a temp home; only the code-host reads
(`pr_head`, `pr_status`, `review_state`) and the push delivery are faked. Fixtures are synthetic.
"""
from __future__ import annotations

import pytest

from conftest import make_origin_and_repo
from maestro import cli, config, event_log, notify, ops, providers, review, store
from maestro import dispatcher as disp
from maestro import snapshot as snap_mod
from maestro.sessions import DryRunSessions
from test_review_kind import URL, _push_pr_head

KEY = "T-60"


class VCS:
    def __init__(self, sha, fork=False):
        self.sha, self.fork, self.state = sha, fork, "OPEN"

    def pr_head(self, pr_number, repo=None, env=None):
        return {"head_sha": self.sha, "head_ref": "f", "is_fork": self.fork, "error": None,
                "title": None, "author": "octo", "state": self.state, "draft": False,
                "base_ref": "main", "additions": 1, "deletions": 1, "changed_files": 1}

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": self.state, "mergeable": "MERGEABLE", "head_sha": self.sha,
                "ci_state": "passing", "failing_checks": [], "draft": False}

    def review_state(self, pr_number, review_id, repo=None, env=None):
        return {"ok": True, "state": "PENDING"}


class Spawns(DryRunSessions):
    """Records every spawn so a sweep that launches a session is visible."""
    spawned: list = []

    def spawn(self, *a, **kw):
        type(self).spawned.append((a, kw))
        return super().spawn(*a, **kw)


@pytest.fixture
def board(home, tmp_path, monkeypatch):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        '[providers]\nvcs = "github_cli"\n'
        f'[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    vcs = VCS(_push_pr_head(origin, repo, tmp_path, "pr-one"))
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    pushes: list = []
    monkeypatch.setattr(notify, "push", lambda cfg, key, event, **kw: pushes.append((key, event, kw)) or True)
    Spawns.spawned = []
    return home, vcs, origin, repo, tmp_path, pushes


def _cfg(home):
    return config.load(str(home))


def _ask(home, vcs, **kw):
    return ops.start_review(_cfg(home), URL, vcs=vcs, via="cli", **kw)


def _briefed(home, vcs):
    """A review at brief-ready: round started, tests skipped, brief written."""
    key = _ask(home, vcs)["key"]
    ops.set_phase(_cfg(home), key, disp.Phase.QA, actor="d", force=True)
    ops.write_review_brief(_cfg(home), key, summary="s", checked="c", decisions="d",
                           verdict="comment", reading_order="r")
    assert review.load(home, key).status == review.ReviewStatus.BRIEF_READY
    return key


def _sweep(home, now):
    return disp.dispatch(_cfg(home), Spawns(), now=now)


def _types(home, key):
    return [e["type"] for e in event_log.read(home, key)]


def _run(home, *argv):
    return cli.main(["--home", str(home), "reviews", *argv])


def _refused(home, capsys, *argv, match):
    capsys.readouterr()
    assert _run(home, *argv) != 0
    assert match in capsys.readouterr().err


def test_moved_head_is_flagged_once_with_one_push(board):
    home, vcs, origin, repo, tmp, pushes = board
    key = _briefed(home, vcs)
    _sweep(home, 10_000)
    assert not review.load(home, key).stale and pushes == []
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    _sweep(home, 20_000)
    assert review.load(home, key).stale
    assert [(k, e) for k, e, _ in pushes] == [(key, "updated")]
    before = _types(home, key)
    _sweep(home, 30_000)
    _sweep(home, 40_000)
    assert _types(home, key) == before and len(pushes) == 1


def test_rereview_starts_a_round_without_any_generic_session(board):
    home, vcs, origin, repo, tmp, _p = board
    key = _briefed(home, vcs)
    old = snap_mod.load(home, key).review_head_sha
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    _sweep(home, 20_000)
    assert _run(home, "rereview", key) == 0
    snap = snap_mod.load(home, key)
    assert snap.review_head_sha == vcs.sha != old and snap.review_round == 2
    assert snap.open_questions == {} and snap.phase == "ready"
    assert not review.load(home, key).stale
    # the order: round, then the superseding answer, then the phase change
    types = [t for t in _types(home, key) if t in ("ReviewRoundStarted", "QuestionAnswered", "PhaseChanged")]
    assert types[-3:] == ["ReviewRoundStarted", "QuestionAnswered", "PhaseChanged"]
    _sweep(home, 30_000)
    assert not [s for s in Spawns.spawned]


def test_sweep_between_answer_and_phase_change_never_spawns(board):
    """A crash after the superseding answer leaves awaiting-human with an answered question."""
    home, vcs, origin, repo, tmp, _p = board
    key = _briefed(home, vcs)
    (qid,) = snap_mod.load(home, key).open_questions
    event_log.append(home, key, "QuestionAnswered",
                     {"qid": qid, "answer": ops.REVIEW_SUPERSEDED_ANSWER}, actor="human")
    snap_mod.rebuild(home, key)
    assert snap_mod.load(home, key).phase == "awaiting-human"
    _sweep(home, 10_000)
    assert not Spawns.spawned
    assert snap_mod.load(home, key).phase == "ready"


def test_rereview_refuses_without_new_commits_and_with_a_pending_review(board, capsys):
    home, vcs, origin, repo, tmp, _p = board
    key = _briefed(home, vcs)
    _refused(home, capsys, "rereview", key, match="no new commits")
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    event_log.append(home, key, "ReviewPendingCreated",
                     {"review_id": "9", "pr_number": 7, "head_sha": snap_mod.load(home, key).review_head_sha},
                     actor="human")
    snap_mod.rebuild(home, key)
    _refused(home, capsys, "rereview", key, match="submit or delete your pending review first")
    assert snap_mod.load(home, key).review_round == 1


def test_new_request_at_a_new_head_starts_a_round(board):
    home, vcs, origin, repo, tmp, _p = board
    key = _briefed(home, vcs)
    assert _ask(home, vcs)["reason"] == "question open"  # same head: unchanged behaviour
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    res = _ask(home, vcs)
    assert res["started"] and res["key"] == key
    snap = snap_mod.load(home, key)
    assert snap.review_round == 2 and snap.open_questions == {} and snap.phase == "ready"
    _sweep(home, 30_000)
    assert not Spawns.spawned


def test_new_request_with_a_pending_review_starts_no_round_and_names_it(board):
    home, vcs, origin, repo, tmp, pushes = board
    key = _briefed(home, vcs)
    head = snap_mod.load(home, key).review_head_sha
    event_log.append(home, key, "ReviewPendingCreated",
                     {"review_id": "9", "pr_number": 7, "head_sha": head}, actor="human")
    event_log.append(home, key, "PhaseChanged", {"phase": "awaiting-ci", "reason": "x"}, actor="human")
    snap_mod.rebuild(home, key)
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    res = _ask(home, vcs)
    assert res["started"] is False and res["reason"] == "pending review"
    snap = snap_mod.load(home, key)
    assert snap.review_round == 1 and snap.review_head_sha == head
    assert review.load(home, key).stale
    (k, event, kw), = pushes
    assert (k, event) == (key, "asked-again") and "pending review" in kw["text"]


def test_retry_after_binding_starts_the_round_and_refuses_while_unbound(home, tmp_path, monkeypatch, capsys):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text('[providers]\nvcs = "github_cli"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    vcs = VCS(_push_pr_head(origin, repo, tmp_path, "pr-one"))
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    res = ops.start_review(_cfg(home), URL, vcs=vcs, via="cli")
    key = res["key"]
    assert res["reason"] == "unbound"
    assert review.load(home, key).status == review.ReviewStatus.BLOCKED
    _refused(home, capsys, "retry", key, match="still not bound")
    assert snap_mod.load(home, key).review_round == 0
    (home / "config.toml").write_text(
        '[providers]\nvcs = "github_cli"\n'
        f'[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    assert _run(home, "retry", key) == 0
    snap = snap_mod.load(home, key)
    assert snap.review_round == 1 and snap.review_head_sha == vcs.sha
    assert snap.open_questions == {} and snap.phase == "ready" and snap.repo == "widgets"
    assert store.worktree_path(home, key).exists()
    _refused(home, capsys, "retry", key, match="needs a blocked review")


def test_retry_refuses_while_the_pr_is_from_a_fork(board, capsys):
    home, vcs, *_ = board
    vcs.fork = True
    res = _ask(home, vcs)
    assert res["reason"] == "fork"
    _refused(home, capsys, "retry", res["key"], match="still from a fork")


def test_reviews_listing_marks_a_stale_review(board, capsys):
    home, vcs, origin, repo, tmp, _p = board
    key = _briefed(home, vcs)
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    _sweep(home, 20_000)
    capsys.readouterr()
    _run(home)
    assert "STALE" in capsys.readouterr().out
