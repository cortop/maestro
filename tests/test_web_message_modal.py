"""Web answer/message modals: fragment routes, real POSTs over a temp home, dismissal and SSE-safety."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import inbox  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


@pytest.fixture
def web(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": "Which colour?"})
    seed_ticket(home, "T-3", "working", phase="implementing")
    app = create_app(home, nudge_on_action=False)
    with TestClient(app) as c:
        c.headers[TOKEN_HEADER] = app.state.guard.token
        yield c, home


def test_answer_modal_fragment_is_dialog_with_textarea(web):
    c, _ = web
    r = c.get("/t/T-1/modal/answer", params={"ctx": "now", "qid": "q1"})
    assert r.status_code == 200
    assert "<dialog" in r.text and "<textarea" in r.text and "data-modal-cancel" in r.text
    assert 'name="qid" value="q1"' in r.text and 'hx-post="/t/T-1/answer"' in r.text


def test_message_modal_fragment_and_triggers(web):
    c, _ = web
    r = c.get("/t/T-3/modal/message", params={"ctx": "ticket"})
    assert "<dialog" in r.text and "<textarea" in r.text and "data-modal-cancel" in r.text
    assert c.get("/t/T-3/modal/bogus").status_code == 404
    assert c.get("/t/T-9/modal/message").status_code == 404
    assert 'hx-get="/t/T-3/modal/message' in c.get("/t/T-3/head").text
    assert "<textarea" not in c.get("/t/T-3/head").text
    page = c.get("/now/decide").text
    assert 'hx-get="/t/T-1/modal/answer?ctx=now&qid=q1"' in page and "<textarea" not in page


def test_answer_submit_appends_inbox_and_refreshes(web):
    c, home = web
    r = c.post("/t/T-1/answer", data={"ctx": "now", "qid": "q1", "modal": "1", "text": "blue\nnot red"})
    assert r.status_code == 200 and 'data-key="T-1"' not in r.text and "<dialog" not in r.text
    cmd = inbox.pending(home, "T-1")[0]
    assert cmd["command"] == "ans" and cmd["args"] == {"text": "blue\nnot red", "qid": "q1"}


def test_message_submit_preserves_newlines(web):
    c, home = web
    r = c.post("/t/T-3/message", data={"ctx": "ticket", "modal": "1", "text": "line one\nline two\n\nline four"})
    assert r.status_code == 200
    cmd = inbox.pending(home, "T-3")[0]
    assert cmd["args"]["text"] == "line one\nline two\n\nline four"


@pytest.mark.parametrize("key,action,ctx", [("T-1", "answer", "now"), ("T-3", "message", "ticket")])
def test_blank_submission_rejected_inline(web, key, action, ctx):
    c, home = web
    r = c.post(f"/t/{key}/{action}", data={"ctx": ctx, "qid": "q1", "modal": "1", "text": " \n "})
    assert r.status_code == 409 and r.headers["HX-Retarget"] == "#modal-host"
    assert "<dialog" in r.text and "must not be empty" in r.text
    assert inbox.pending(home, key) == []


def test_dismissal_wired_and_modal_outside_swapped_regions(web):
    c, _ = web
    js = c.get("/static/modal.js").text
    assert "data-modal-cancel" in js and '"cancel"' in js and 'id === "action-modal"' in js
    page = c.get("/").text
    assert page.index('id="modal-host"') > page.index("</main>")  # outside <main>: no refresh swaps it
    assert "<dialog" not in page
