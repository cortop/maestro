"""T-293: every review request is recorded (who, when, where) and the PR's title/size is observed.

Real CLI, real git, real queue drain; only the code-host read (`pr_head`) is faked.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from conftest import make_origin_and_repo, seed_review_ticket
from maestro import cli, config, event_log, human_actions, ops, providers, review, slack_watcher, store
from test_review_kind import URL, _push_pr_head

TS = "1700000000.123456"
PERMALINK = "https://chat.example/archives/C0SYNTH/p1700000000123456"


class PrVCS:
    def __init__(self, sha, **extra):
        self.sha, self.calls = sha, 0
        self.fields = {"title": None, "author": "octo", "state": "OPEN", "draft": False,
                       "base_ref": "main", "additions": 10, "deletions": 2, "changed_files": 3}
        self.fields.update(extra)

    def pr_head(self, pr_number, repo=None, env=None):
        self.calls += 1
        return {"head_sha": self.sha, "head_ref": "feature", "is_fork": False, "error": None,
                **self.fields}


@pytest.fixture
def board(home, tmp_path, monkeypatch):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        '[maestro]\nslack_channel = "C0SYNTH"\nslack_user_id = "U1"\n'
        'slack_read_tools = ["mcp__chat__read"]\nslack_active_hours = ""\n'
        'slack_workspace_url = "https://chat.example"\n'
        f'[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    sha = _push_pr_head(origin, repo, tmp_path, "pr-one")
    vcs = PrVCS(sha)
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    return home, vcs


def _only(home):
    (r,) = review.list_reviews(home)
    return r


def _events(home, key, type_):
    return [e for e in event_log.read(home, key) if e["type"] == type_]


def _ping(home, **msg):
    cfg = config.load(str(home))
    base = {"ts": TS, "text": f"<@U1> please review {URL}"}
    slack_watcher.intake(cfg, [{**base, **msg}], now=1700000100)
    return cfg, slack_watcher.drain(cfg)


def test_slack_ping_records_sender_time_and_place(board):
    home, _ = board
    _, out = _ping(home, user="U9", user_name="Alice")
    assert len(out["filed"]) == 1
    r = _only(home)
    assert r.requested_by == "Alice" and r.via == "slack" and not r.requested_at_inferred
    assert r.requested_at.startswith("2023-11-14T22:13:20")
    assert r.where_label == "Slack #C0SYNTH" and r.where_url == PERMALINK
    ev = _events(home, r.key, "ReviewRequested")[0]["payload"]
    assert ev["requester"] == {"id": "U9", "name": "Alice"}
    assert ev["where"] == {"channel": "C0SYNTH", "message_ts": TS, "permalink": PERMALINK}
    assert ev["pr"] == {"slug": "acme/widgets", "number": 7, "url": URL} and ev["head_sha"]


def test_ping_without_sender_still_files(board):
    home, _ = board
    _ping(home)
    r = _only(home)
    assert r.requested_by is None and r.request_count == 1 and r.round == 1
    assert _events(home, r.key, "ReviewRequested")[0]["payload"]["requester"] is None


def test_second_ping_while_question_open_records_without_round(board):
    home, _ = board
    _ping(home, user="U9", user_name="Alice")
    r = _only(home)
    ops.ask(config.load(str(home)), r.key, "brief ready?", qid="review-brief-x", actor="reconciler")
    cfg = config.load(str(home))
    slack_watcher.intake(cfg, [{"ts": "1700000500.000100", "text": f"<@U1> again {URL}",
                                "user": "U8", "user_name": "Bob"}], now=1700000600)
    slack_watcher.drain(cfg)
    r = review.load(home, r.key)
    assert r.request_count == 2 and r.round == 1
    assert [q["requester"]["name"] for q in r.requests] == ["Alice", "Bob"]
    assert r.requested_by == "Alice" and r.last_request["requester"]["name"] == "Bob"
    assert len(_events(home, r.key, "ReviewRoundStarted")) == 1


def test_cli_web_and_tui_record_their_surface(board, capsys):
    home, vcs = board
    assert cli.main(["review", URL, "--requested-by", "Alice"]) == 0
    r = _only(home)
    assert (r.via, r.requested_by) == ("cli", "Alice")
    # same head again: nothing new
    assert cli.main(["review", URL, "--requested-by", "Alice"]) == 0
    assert review.load(home, r.key).request_count == 1
    cfg = config.load(str(home))
    human_actions.start_review(cfg, URL, vcs=vcs)
    human_actions.start_review(cfg, URL, vcs=vcs, via="tui")
    assert [q["via"] for q in review.load(home, r.key).requests] == ["cli", "web", "tui"]


def test_tui_modal_records_via_tui(board):
    home, vcs = board
    from tui_support import _make_app
    from textual.widgets import Input

    async def _inner():
        app = _make_app(home)
        app._vcs = vcs
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("escape")
            app.action_review_pr()
            await pilot.pause()
            app.screen.query_one("#inbox-input", Input).value = URL
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None
    asyncio.run(_inner())
    assert _only(home).via == "tui"


def test_web_form_records_via_web(board):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from maestro.web.app import create_app
    from maestro.web.guard import TOKEN_HEADER
    home, vcs = board
    app = create_app(home, nudge_on_action=False, vcs=vcs)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        assert c.post("/new/review", data={"url": URL}).status_code == 200
    assert _only(home).via == "web"


def test_pr_facts_are_exposed_cleaned_and_capped(board):
    home, vcs = board
    vcs.fields["title"] = "Fix\x1b[31m the\x00 thing\n" + "x" * 300
    assert cli.main(["review", URL, "--requested-by", "Al\x07ice\n" + "n" * 200]) == 0
    r = _only(home)
    assert r.title.startswith("Fix [31m the thing xxx") and len(r.title) == 200
    assert "\x1b" not in r.title and "\x00" not in r.title
    assert r.requested_by.startswith("Al ice n") and len(r.requested_by) == 80
    assert (r.draft, r.additions, r.deletions, r.changed_files, r.pr_state) == (False, 10, 2, 3, "OPEN")


def test_unchanged_pr_appends_one_observation(board):
    home, vcs = board
    cfg = config.load(str(home))
    ops.start_review(cfg, URL, vcs=vcs)
    key = _only(home).key
    ops.start_review(cfg, URL, vcs=vcs)
    assert len(_events(home, key, "ReviewPrObserved")) == 1
    vcs.fields["additions"] = 11
    ops.start_review(cfg, URL, vcs=vcs)
    assert len(_events(home, key, "ReviewPrObserved")) == 2
    assert review.load(home, key).additions == 11


def test_spec_and_ticket_title_carry_no_pr_text(board):
    home, vcs = board
    vcs.fields["title"] = "SECRET-PR-TITLE"
    cli.main(["review", URL, "--requested-by", "Mallory"])
    key = _only(home).key
    spec = store.spec_path(home, key).read_text()
    created = _events(home, key, "TicketCreated")[0]["payload"]
    for blob in (spec, json.dumps(created)):
        assert "SECRET-PR-TITLE" not in blob and "Mallory" not in blob
    assert created["title"] == "Review acme/widgets#7"


def test_reviews_listing_shows_title_requester_and_time(home, capsys):
    seed_review_ticket(home, requester="Alice", requested_ts="2026-01-02T03:04:05+00:00",
                       title="Add parser")
    assert cli.main(["--home", str(home), "reviews"]) == 0
    out = capsys.readouterr().out
    assert "Add parser" in out and "Alice" in out and "2026-01-02T03:04:05" in out
