"""Review notifications: kind/event in the command env and webhook, silent close, notify.push."""
from __future__ import annotations

import json

import pytest

from maestro import notify
from tests.conftest import seed_ticket
from tests.test_review_model import seed


@pytest.fixture
def rec(home, cfg, monkeypatch, tmp_path):
    """notify_command appends its env to a file; webhook posts are captured."""
    out = tmp_path / "fired.jsonl"
    cfg.notify_command = (
        'python3 -c "import os,json;print(json.dumps({k:os.environ[k] for k in '
        f"['KEY','PHASE','KIND','EVENT','TITLE','URL','REQUESTER']}}))\" >> {out}"
    )
    posts = []
    monkeypatch.setattr(notify, "_post_webhook", lambda url, payload: posts.append(payload))
    cfg.webhook_urls = ["http://hook.invalid/x"]

    def fired():
        return [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    return fired, posts


def test_review_brief_ready_once(home, cfg, rec):
    fired, posts = rec
    seed(home, "r-1", "awaiting-human", questions=["review-brief-r-1-abc"], brief=True)
    assert notify.maybe_notify(cfg, 0) == ["r-1"]
    assert notify.maybe_notify(cfg, 1) == []
    [f] = fired()
    assert (f["KIND"], f["EVENT"], f["PHASE"], f["KEY"]) == ("review", "brief-ready",
                                                              "awaiting-human", "r-1")
    assert f["URL"] == "https://github.com/acme/widgets/pull/7"
    assert posts[0]["kind"] == "review" and posts[0]["event"] == "brief-ready"


def test_done_review_silent_ticket_still_fires(home, cfg, rec):
    fired, _ = rec
    seed(home, "r-1", "done", reason="merged")
    seed_ticket(home, "T-1", "t", phase="done")
    assert notify.maybe_notify(cfg, 0) == ["T-1"]
    [f] = fired()
    assert (f["KEY"], f["KIND"], f["EVENT"]) == ("T-1", "ticket", "done")


def test_blocked_and_failed_reviews(home, cfg, rec):
    fired, _ = rec
    seed(home, "r-1", "awaiting-human", questions=["bind-repo"])
    seed(home, "r-2", "degraded")
    notify.maybe_notify(cfg, 0)
    assert {f["KEY"]: f["EVENT"] for f in fired()} == {"r-1": "blocked", "r-2": "failed"}


def test_push_dedupes_and_reaches_both(home, cfg, rec):
    fired, posts = rec
    seed(home, "r-1", "qa")
    kw = dict(title="T; $(touch pwned)", url="https://x/y", requester="bob", text="hi",
              dedupe="d1")
    assert notify.push(cfg, "r-1", "requested", **kw) is True
    assert notify.push(cfg, "r-1", "requested", **kw) is False
    assert notify.push(cfg, "r-1", "requested", **{**kw, "dedupe": "d2"}) is True
    f = fired()
    assert len(f) == 2 and f[0]["EVENT"] == "requested" and f[0]["KIND"] == "review"
    assert f[0]["TITLE"] == "T; $(touch pwned)" and f[0]["REQUESTER"] == "bob"
    assert posts[0]["event"] == "requested" and posts[0]["kind"] == "review"
    assert posts[0]["requester"] == "bob" and posts[0]["question"] == "hi"
    # the push cursor entry does not disturb the sweep's per-key phase cursor
    assert notify.maybe_notify(cfg, 2) == []
