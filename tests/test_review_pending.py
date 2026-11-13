"""T-253: a guarded POST turns the kept review findings into one pending GitHub review.

Real web app, real `human_actions`, real dispatcher sweeps over a temp home; only the code-host
boundary (`FakeVCS`, and `gh` itself for the provider test) and the `claude -p` spawn are faked.
Fixtures are synthetic.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_review_ticket  # noqa: E402
from maestro import config, event_log, human_actions, providers, store  # noqa: E402
from maestro import dispatcher as disp  # noqa: E402
from maestro import snapshot as snap_mod  # noqa: E402
from maestro.providers import cli as gh_cli  # noqa: E402
from maestro.sessions import DryRunSessions  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402

KEY, HEAD = "T-60", "a" * 40


class FakeVCS:
    """The code host: records every create call and answers reads from its attributes."""

    def __init__(self, *, head=HEAD, pending=None, lines=None, state="OPEN", review="PENDING"):
        self.head, self.pending, self.state, self.review = head, pending, state, review
        self.lines = {"src/a.py": [2, 3, 4]} if lines is None else lines
        self.created: list[dict] = []

    def pr_head(self, pr_number, repo=None, env=None):
        return {"head_sha": self.head, "state": self.state, "is_fork": False, "error": None}

    def pending_review(self, pr_number, repo=None, env=None):
        return {"ok": True, "review_id": self.pending}

    def pr_diff_lines(self, pr_number, repo=None, env=None):
        return {"ok": True, "lines": self.lines}

    def create_pending_review(self, pr_number, head_sha, body, comments, repo=None, env=None):
        self.created.append({"pr": pr_number, "repo": repo, "head_sha": head_sha, "body": body,
                             "comments": comments})
        return {"ok": True, "id": "9001"}

    def review_state(self, pr_number, review_id, repo=None, env=None):
        return {"ok": True, "state": self.review}

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": self.state, "mergeable": "MERGEABLE", "head_sha": self.head,
                "ci_state": "passing", "failing_checks": [], "draft": False}


@pytest.fixture
def board(home):
    seed_review_ticket(home)
    vcs = FakeVCS()
    app = create_app(home, nudge_on_action=False, vcs=vcs)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home, vcs


def _form(**over):
    data = {"head": HEAD, "include-0": "1", "comment-0": "Please guard the empty case, then retest.",
            "include-1": "1", "comment-1": "Rename `x` to `count`."}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def test_post_creates_pending_review_with_edited_text_and_body_fallback(board):
    c, home, vcs = board
    r = c.post(f"/t/{KEY}/review/pending", data=_form())
    assert r.status_code == 200, r.text
    assert vcs.created == [{
        "pr": 7, "repo": "acme/widgets", "head_sha": HEAD,
        "body": "`src/b.py:9`\n\nRename `x` to `count`.",
        "comments": [{"path": "src/a.py", "line": 3, "body": "Please guard the empty case, then retest."}]}]
    snap = snap_mod.load(home, KEY)
    assert snap.review_pending == {"review_id": "9001", "pr_number": 7, "head_sha": HEAD,
                                   "inline": 1, "in_body": 1}
    assert snap.phase == "awaiting-ci" and not snap.open_questions
    assert "waiting on GitHub" in r.text


def test_only_included_findings_are_sent_verbatim(board):
    c, home, vcs = board
    text = "  Keep   the spacing\n\n  and <b>markup</b>  "
    r = c.post(f"/t/{KEY}/review/pending", data=_form(**{"include-1": None, "comment-0": text}))
    assert r.status_code == 200
    assert vcs.created[0]["comments"] == [{"path": "src/a.py", "line": 3, "body": text}]
    assert vcs.created[0]["body"] == ""


def test_existing_pending_review_is_never_touched(board):
    c, home, vcs = board
    vcs.pending = "55"
    before = event_log.read(home, KEY)
    r = c.post(f"/t/{KEY}/review/pending", data=_form())
    assert r.status_code == 409 and "already have a pending review" in r.text
    assert vcs.created == [] and event_log.read(home, KEY) == before
    assert snap_mod.load(home, KEY).phase == "awaiting-human"


@pytest.mark.parametrize("form,vcs_kw,msg", [
    ({"head": "b" * 40}, {}, "changed since this page loaded"),
    ({}, {"head": "c" * 40}, "new commits"),
    ({}, {"state": "MERGED"}, "not open"),
    ({"include-0": None, "include-1": None}, {}, "no finding is included"),
    ({"comment-0": "  "}, {}, "empty comment"),
])
def test_refusals_create_nothing(home, form, vcs_kw, msg):
    seed_review_ticket(home)
    vcs = FakeVCS(**vcs_kw)
    app = create_app(home, nudge_on_action=False, vcs=vcs)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        r = c.post(f"/t/{KEY}/review/pending", data=_form(**form))
    assert r.status_code == 409 and msg in r.text
    assert vcs.created == [] and snap_mod.load(home, KEY).review_pending is None


def test_a_gh_failure_shows_ghs_own_message(home, monkeypatch):
    seed_review_ticket(home)
    monkeypatch.setattr(gh_cli, "_run", lambda cmd, timeout=60, env=None: (
        1, "", "HTTP 502: Bad Gateway (https://api.github.com/graphql)\n"))
    app = create_app(home, nudge_on_action=False, vcs=gh_cli.GitHubCliVCS({}))
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        r = c.post(f"/t/{KEY}/review/pending", data=_form())
    assert r.status_code == 409
    assert ("could not read the pull request on GitHub "
            "(unknown: HTTP 502: Bad Gateway (https://api.github.com/graphql))") in r.text
    assert snap_mod.load(home, KEY).review_pending is None


def test_post_is_behind_the_write_guard(board):
    c, home, vcs = board
    del c.headers[TOKEN_HEADER]
    assert c.post(f"/t/{KEY}/review/pending", data=_form()).status_code == 403
    assert vcs.created == []


def test_read_only_mode_mounts_no_route(home):
    seed_review_ticket(home)
    with TestClient(create_app(home, read_only=True, vcs=FakeVCS())) as c:
        assert c.post(f"/t/{KEY}/review/pending", data=_form()).status_code in (404, 405)


def test_button_shows_only_while_the_brief_awaits_the_human(board):
    c, home, _ = board
    html = c.get(f"/t/{KEY}/review").text
    assert 'id="create-pending"' in html and f'hx-post="/t/{KEY}/review/pending"' in html
    assert 'name="comment-0"' in html and 'name="include-1"' in html
    c.post(f"/t/{KEY}/review/pending", data=_form())
    assert 'id="create-pending"' not in c.get(f"/t/{KEY}/review").text


def test_no_agent_session_runs_after_creation(board):
    c, home, vcs = board
    assert c.post(f"/t/{KEY}/review/pending", data=_form()).status_code == 200
    event_log.append(home, KEY, "SpecObserved", {"spec_hash": disp.spec_hash_on_disk(home, KEY)},
                     actor="d")  # the fixture's placeholder hash would otherwise wake the ticket
    snap_mod.rebuild(home, KEY)
    sessions = DryRunSessions()
    disp.dispatch(config.load(str(home)), sessions, now=10_000)
    assert sessions.spawned == []
    assert not [e for e in event_log.read(home, KEY) if e["type"] == "SessionSpawned"]


def test_human_action_needs_a_brief_and_a_review_ticket(home):
    cfg = config.Config(home=home)
    seed_review_ticket(home, "T-61", brief=False, phase="qa")
    with pytest.raises(store.MaestroError, match="brief is not ready"):
        human_actions.create_pending_review(cfg, "T-61", [{"index": 0, "comment": "x"}], vcs=FakeVCS())


@pytest.fixture
def polled(home, monkeypatch):
    (home / "config.toml").write_text('[providers]\nvcs = "github_cli"\n')
    seed_review_ticket(home)
    vcs = FakeVCS()
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    human_actions.create_pending_review(config.load(str(home)), KEY,
                                        [{"index": 0, "comment": "ok"}], vcs=vcs)
    return home, vcs


def _sweep(home, now):
    disp.dispatch(config.load(str(home)), DryRunSessions(), now=now)
    return snap_mod.load(home, KEY)


def test_still_pending_review_keeps_the_ticket_waiting(polled):
    home, vcs = polled
    assert _sweep(home, 10_000).phase == "awaiting-ci"


@pytest.mark.parametrize("kw", [{"review": "COMMENTED"}, {"review": "CHANGES_REQUESTED"},
                                {"state": "MERGED"}, {"state": "CLOSED"}])
def test_submitted_merged_or_closed_finishes_the_ticket(polled, kw):
    home, vcs = polled
    for k, v in kw.items():
        setattr(vcs, k, v)
    assert _sweep(home, 10_000).phase == "done"


# --- the gh provider: exact requests -----------------------------------------------------------

def test_gh_provider_posts_a_review_without_an_event(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=60, env=None):
        seen["cmd"] = cmd
        seen["payload"] = json.loads(open(cmd[cmd.index("--input") + 1]).read())
        return 0, json.dumps({"id": 42, "state": "PENDING"}), ""

    monkeypatch.setattr(gh_cli, "_run", fake_run)
    out = gh_cli.GitHubCliVCS({}).create_pending_review(
        7, HEAD, "body", [{"path": "a.py", "line": 3, "body": "text \"quoted\""}], "acme/widgets")
    assert out == {"ok": True, "id": "42"}
    assert seen["cmd"][:3] == ["gh", "api", "repos/acme/widgets/pulls/7/reviews"]
    assert seen["payload"] == {"commit_id": HEAD, "body": "body", "comments": [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "text \"quoted\""}]}
    assert "event" not in seen["payload"]


def test_gh_provider_reads_pending_review_diff_lines_and_state(monkeypatch):
    patch = "@@ -1,3 +1,4 @@\n a\n-b\n+c\n+d\n e\n@@ -10,2 +11,2 @@\n x\n+y\n\\ No newline at end of file"
    answers = {
        "reviews": json.dumps([{"id": 1, "state": "COMMENTED"}]) + json.dumps([{"id": 2, "state": "PENDING"}]),
        "files": json.dumps([{"filename": "a.py", "patch": patch}, {"filename": "big.bin"}]),
    }

    def fake_run(cmd, timeout=60, env=None):
        if cmd[-1].endswith("/reviews/2"):
            return 1, "", "gh: Not Found (HTTP 404)"
        return 0, answers[cmd[-1].rsplit("/", 1)[1]], ""

    monkeypatch.setattr(gh_cli, "_run", fake_run)
    vcs = gh_cli.GitHubCliVCS({})
    assert vcs.pending_review(7, "o/r") == {"ok": True, "review_id": "2"}
    assert vcs.pr_diff_lines(7, "o/r") == {"ok": True, "lines": {"a.py": [1, 2, 3, 4, 11, 12], "big.bin": []}}
    assert vcs.review_state(7, "2", "o/r") == {"ok": True, "state": "DELETED"}
