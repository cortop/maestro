"""T-295: a closed review records why it closed, and a request after the close is handled.

Real CLI, real git worktrees, real dispatcher sweeps over a temp home; only the code-host reads
(`pr_head`, `pr_status`, `review_state`) and the push delivery are faked. Fixtures are synthetic.
"""
from __future__ import annotations

import threading

import pytest

from conftest import make_origin_and_repo, seed_review_ticket
from maestro import cli, config, event_log, notify, ops, providers, review, store
from maestro import dispatcher as disp
from maestro import snapshot as snap_mod
from maestro.sessions import DryRunSessions
from test_review_kind import URL, _push_pr_head

KEY = "T-60"


class VCS:
    def __init__(self, sha="a" * 40, state="OPEN", review="PENDING"):
        self.sha, self.state, self.review = sha, state, review

    def pr_head(self, pr_number, repo=None, env=None):
        return {"head_sha": self.sha, "head_ref": "f", "is_fork": False, "error": None,
                "title": None, "author": "octo", "state": self.state, "draft": False,
                "base_ref": "main", "additions": 1, "deletions": 1, "changed_files": 1}

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": self.state, "mergeable": "MERGEABLE", "head_sha": self.sha,
                "ci_state": "passing", "failing_checks": [], "draft": False}

    def review_state(self, pr_number, review_id, repo=None, env=None):
        return {"ok": True, "state": self.review}


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
    monkeypatch.setattr(notify, "push", lambda cfg, key, event, **kw: pushes.append((key, event)) or True)
    return home, vcs, origin, repo, tmp_path, pushes


def _cfg(home):
    return config.load(str(home))


def _ask(home, vcs):
    return ops.start_review(_cfg(home), URL, vcs=vcs, via="cli")


def _types(home, key):
    return [e["type"] for e in event_log.read(home, key)]


def _outcome(home, key):
    (e,) = [e for e in event_log.read(home, key) if e["type"] == "ReviewClosed"]
    return e["payload"]


def _closed_before_done(home, key):
    types = _types(home, key)
    return types.index("ReviewClosed") < max(i for i, t in enumerate(types) if t == "PhaseChanged")


def _sweep(home, key, now):
    event_log.append(home, key, "SpecObserved", {"spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    snap_mod.rebuild(home, key)
    disp.dispatch(_cfg(home), DryRunSessions(), now=now)


@pytest.mark.parametrize("state,outcome", [("MERGED", "pr-merged"), ("CLOSED", "pr-closed")])
def test_merged_or_closed_pr_records_outcome_before_done(board, state, outcome):
    home, vcs, *_ = board
    seed_review_ticket(home, phase="qa")
    vcs.state = state
    _sweep(home, KEY, 10_000)
    assert snap_mod.load(home, KEY).phase == "done"
    assert _outcome(home, KEY) == {"outcome": outcome}
    assert _closed_before_done(home, KEY)
    r = review.load(home, KEY)
    assert r.outcome == outcome and r.closed_at


def test_submitted_pending_review_records_outcome_and_state(board):
    home, vcs, *_ = board
    seed_review_ticket(home, phase="awaiting-ci")
    event_log.append(home, KEY, "ReviewPendingCreated",
                     {"review_id": "9", "pr_number": 7, "head_sha": "a" * 40}, actor="human")
    event_log.append(home, KEY, "PhaseChanged", {"phase": "awaiting-ci", "reason": "x"}, actor="human")
    snap_mod.rebuild(home, KEY)
    vcs.review = "APPROVED"
    _sweep(home, KEY, 10_000)
    assert _outcome(home, KEY) == {"outcome": "submitted", "review_state": "APPROVED"}
    assert _closed_before_done(home, KEY)


def test_reviewed_records_dismissed_and_removes_worktree(board, capsys):
    home, vcs, *_ = board
    key = _ask(home, vcs)["key"]
    wt = store.worktree_path(home, key)
    assert wt.exists()
    assert cli.main(["--home", str(home), "reviewed", key]) == 0
    assert _outcome(home, key) == {"outcome": "dismissed"}
    assert _closed_before_done(home, key)
    assert snap_mod.load(home, key).phase == "done" and not wt.exists()
    assert cli.main(["--home", str(home), "reviewed", key]) == 0  # idempotent: no second event
    assert _types(home, key).count("ReviewClosed") == 1


def test_same_head_request_after_close_is_recorded_and_pushed(board):
    home, vcs, _o, _r, _t, pushes = board
    key = _ask(home, vcs)["key"]
    cli.main(["--home", str(home), "reviewed", key])
    res = _ask(home, vcs)
    assert res["key"] == key and res["minted"] is False and res["started"] is False
    assert _types(home, key).count("ReviewRequested") == 2
    assert pushes == [(key, "asked-again")]
    assert [r.key for r in review.list_reviews(home, include_closed=True)] == [key]


def test_new_head_after_close_mints_a_linked_review_and_starts_its_round(board):
    home, vcs, origin, repo, tmp, _p = board
    key = _ask(home, vcs)["key"]
    cli.main(["--home", str(home), "reviewed", key])
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    res = _ask(home, vcs)
    assert res["minted"] and res["started"] and res["key"] == f"{key}-2"
    first = [e for e in event_log.read(home, res["key"]) if e["type"] == "ReviewRequested"][0]
    assert first["payload"]["previous_key"] == key
    assert "ReviewRoundStarted" in _types(home, res["key"])
    assert snap_mod.load(home, key).phase == "done"
    new = review.load(home, res["key"])
    assert new.previous_key == key
    assert review.load(home, key).outcome == "dismissed"
    third = _ask(home, vcs)  # same head again: no third review
    assert third["key"] == res["key"] and third["minted"] is False


def test_archived_review_is_never_overwritten(board):
    home, vcs, *_ = board
    key = _ask(home, vcs)["key"]
    cli.main(["--home", str(home), "reviewed", key])
    assert ops.archive_done(_cfg(home)) == [key]
    archived = store.archived_events_path(home, key)
    before = archived.read_bytes()
    res = _ask(home, vcs)
    assert res["minted"] and res["key"] == f"{key}-2" and res["started"]
    assert archived.read_bytes() == before
    assert not store.events_path(home, key).exists()


def test_request_for_a_merged_pr_mints_nothing(board):
    home, vcs, *_ = board
    vcs.state = "MERGED"
    res = _ask(home, vcs)
    assert res["minted"] is False and res["started"] is False and res["key"] is None
    assert disp.list_keys(home) == []


def test_request_for_a_closed_pr_is_recorded_on_the_existing_review(board):
    home, vcs, *_ = board
    key = _ask(home, vcs)["key"]
    vcs.state = "CLOSED"
    res = _ask(home, vcs)
    assert res["key"] == key and res["started"] is False
    assert _types(home, key).count("ReviewRequested") == 1  # same via+head: idempotent
    assert disp.list_keys(home) == [key]


def test_simultaneous_requests_make_one_review(board):
    home, vcs, *_ = board
    errors: list = []

    def go():
        try:
            _ask(home, vcs)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(review.list_reviews(home, include_closed=True)) == 1


def test_reviews_all_shows_outcome_and_previous(board, capsys):
    home, vcs, origin, repo, tmp, _p = board
    key = _ask(home, vcs)["key"]
    cli.main(["--home", str(home), "reviewed", key])
    vcs.sha = _push_pr_head(origin, repo, tmp, "pr-two")
    _ask(home, vcs)
    capsys.readouterr()
    cli.main(["--home", str(home), "reviews", "--all"])
    out = capsys.readouterr().out
    assert "closed: dismissed" in out and f"after {key}" in out


def test_legacy_review_without_the_event_falls_back_to_the_reason(board):
    home, *_ = board
    seed_review_ticket(home, phase="done")
    event_log.append(home, KEY, "PhaseChanged", {"phase": "done", "reason": "pull request merged"}, actor="d")
    snap_mod.rebuild(home, KEY)
    assert review.load(home, KEY).outcome == "merged"
