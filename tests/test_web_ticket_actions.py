"""Web ticket actions: guarded POST routes over a temp home, asserting inbox entries, holds and 409s."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import claims, cli, fleet, inbox  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402

REC = "Shall I proceed?\n   Recommended: Yes, go ahead."


@pytest.fixture
def web(home):
    seed_ticket(home, "T-1", "asks with rec", phase="awaiting-human", questions={"q1": REC})
    seed_ticket(home, "T-2", "stalled", phase="degraded")
    seed_ticket(home, "T-3", "working", phase="implementing")
    seed_ticket(home, "T-4", "finished", phase="done")
    app = create_app(home, nudge_on_action=False)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home


def _pending(home, key):
    return inbox.pending(home, key)


def test_accept_records_marker_and_card_is_gone(web):
    c, home = web
    assert "Accept recommendation" in c.get("/").text
    r = c.post("/t/T-1/answer", data={"ctx": "now", "qid": "q1", "accept": "1"})
    assert r.status_code == 200 and "<html" not in r.text
    cmd = _pending(home, "T-1")[0]
    assert cmd["command"] == "ans"
    assert cmd["args"] == {"text": "Yes, go ahead.", "qid": "q1", "accepted_recommendation": True}
    assert 'data-key="T-1"' not in r.text
    assert 'data-key="T-1"' not in c.get("/now/decide").text


def test_free_text_answer_and_empty_refused(web):
    c, home = web
    r = c.post("/t/T-1/answer", data={"ctx": "now", "qid": "q1", "text": "  "})
    assert r.status_code == 409 and "must not be empty" in r.text and _pending(home, "T-1") == []
    assert c.post("/t/T-1/answer", data={"ctx": "now", "qid": "q1", "text": "do X"}).status_code == 200
    args = _pending(home, "T-1")[0]["args"]
    assert args == {"text": "do X", "qid": "q1"}


def test_accept_without_recommendation_is_409(home):
    seed_ticket(home, "T-1", "plain", phase="awaiting-human", questions={"q1": "Which colour?"})
    app = create_app(home, nudge_on_action=False)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        assert "Accept recommendation" not in c.get("/").text
        r = c.post("/t/T-1/answer", data={"ctx": "now", "qid": "q1", "accept": "1"})
    assert r.status_code == 409 and "no recommendation" in r.text and _pending(home, "T-1") == []


def test_message_and_retry_with_note(web):
    c, home = web
    r = c.post("/t/T-3/message", data={"text": "please hurry"})
    assert r.status_code == 200 and "ticket-actions" in r.text
    assert _pending(home, "T-3")[0]["command"] == "msg"
    assert c.post("/t/T-2/retry", data={"ctx": "now", "text": "try again"}).status_code == 200
    assert [x["command"] for x in _pending(home, "T-2")] == ["retry", "msg"]
    r = c.post("/t/T-3/retry", data={})
    assert r.status_code == 409 and "only applies to degraded" in r.text


def test_discard_and_stop_need_confirm(web):
    c, home = web
    r = c.post("/t/T-2/discard", data={"ctx": "now"})
    assert r.status_code == 409 and "needs confirmation" in r.text and _pending(home, "T-2") == []
    assert c.post("/t/T-2/discard", data={"ctx": "now", "confirm": "1"}).status_code == 200
    assert _pending(home, "T-2")[0]["command"] == "discard"
    r = c.post("/t/T-3/stop", data={})
    assert r.status_code == 409 and "needs confirmation" in r.text
    r = c.post("/t/T-3/stop", data={"confirm": "1"})
    assert r.status_code == 409 and "no claim" in r.text
    seed_ticket(home, "T-5", "stalled too", phase="degraded")
    page = c.get("/").text
    assert "Confirm discard" in page and '<details class="confirm">' in page


def test_hold_and_release_toggle(web):
    c, home = web
    assert ">Hold<" in c.get("/t/T-3").text and ">Release<" not in c.get("/t/T-3").text
    r = c.post("/t/T-3/hold", data={})
    assert r.status_code == 200 and ">Release<" in r.text and ">Hold<" not in r.text
    assert fleet.hold_path(home, "T-3").exists()
    r = c.post("/t/T-3/release", data={})
    assert r.status_code == 200 and ">Hold<" in r.text
    assert not fleet.hold_path(home, "T-3").exists()


def test_actions_shown_only_when_valid(web):
    c, _ = web
    done = c.get("/t/T-4/head").text
    assert "Message agent" not in done and ">Hold<" not in done and "Stop session" not in done
    assert "Message agent" in c.get("/t/T-3/head").text
    assert "Stop session" not in c.get("/t/T-3/head").text  # no live claim


def test_stop_live_session(web):
    c, home = web
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    claims.write_claim(home, "T-3", proc.pid, "reconcile-T-3")
    assert "Stop session" in c.get("/t/T-3/head").text
    r = c.post("/t/T-3/stop", data={"confirm": "1"})
    assert r.status_code == 200
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None


def test_unknown_ticket_and_guard(web):
    c, home = web
    assert c.post("/t/T-99/message", data={"text": "x"}).status_code == 409
    c.headers.pop(TOKEN_HEADER)
    assert c.post("/t/T-3/message", data={"text": "x"}).status_code == 403
    assert _pending(home, "T-3") == []


def test_read_only_mounts_no_routes_and_hides_buttons(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": REC})
    with TestClient(create_app(home, read_only=True)) as c:
        assert "Accept recommendation" not in c.get("/").text
        assert c.post("/t/T-1/answer", data={"qid": "q1", "accept": "1"}).status_code in (404, 405)


def test_nudge_runs_after_inbox_write(home, monkeypatch):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": REC})
    calls = []
    monkeypatch.setattr(cli, "_nudge", lambda cfg: calls.append(1))
    app = create_app(home)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        assert c.post("/t/T-1/answer", data={"qid": "q1", "text": "go"}).status_code == 200
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.05)
    assert calls or not app.state.shell.cfg().nudge_on_human_input
