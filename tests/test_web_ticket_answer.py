"""Web ticket-page decisions: answer, approve/reject, retry/discard, proposal actions and accept-all."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import inbox  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402

REC = "Shall I proceed?\n   Recommended: Yes, go ahead."


def _client(home):
    app = create_app(home, nudge_on_action=False)
    c = TestClient(app)
    c.headers[TOKEN_HEADER] = app.state.guard.token
    return c


def _args(home, key):
    return [(x["command"], x["args"]) for x in inbox.pending(home, key)]


def test_ticket_page_answer_controls_and_inbox(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": REC})
    with _client(home) as c:
        head = c.get("/t/T-1/head").text
        assert "Yes, go ahead." in head and "Accept recommendation" in head and "Write an answer" in head
        r = c.post("/t/T-1/answer", data={"ctx": "ticket", "qid": "q1", "accept": "1"})
        assert r.status_code == 200
    assert _args(home, "T-1") == [("ans", {"text": "Yes, go ahead.", "qid": "q1", "accepted_recommendation": True})]


def test_ticket_page_free_text_answer(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": REC})
    with _client(home) as c:
        assert c.post("/t/T-1/answer", data={"ctx": "ticket", "qid": "q1", "text": "do X"}).status_code == 200
    assert _args(home, "T-1") == [("ans", {"text": "do X", "qid": "q1"})]


def test_approve_reject_buttons_send_tui_text(home):
    seed_ticket(home, "T-1", "asks", phase="awaiting-human", questions={"q1": "Which?"})
    with _client(home) as c:
        head = c.get("/t/T-1/head").text
        assert ">Approve<" in head and ">Reject<" in head
        assert "Accept recommendation" not in head
        c.post("/t/T-1/answer", data={"ctx": "ticket", "qid": "q1", "text": "approve"})
    assert _args(home, "T-1") == [("ans", {"text": "approve", "qid": "q1"})]


def test_degraded_page_has_retry_and_discard(home):
    seed_ticket(home, "T-2", "stalled", phase="degraded")
    with _client(home) as c:
        head = c.get("/t/T-2/head").text
        assert "Retry with a note" in head and "Confirm discard" in head
        assert c.post("/t/T-2/discard", data={"ctx": "ticket"}).status_code == 409
        assert c.post("/t/T-2/retry", data={"ctx": "ticket", "text": "again"}).status_code == 200
        assert c.post("/t/T-2/discard", data={"ctx": "ticket", "confirm": "1"}).status_code == 200
    assert [x[0] for x in _args(home, "T-2")] == ["retry", "msg", "discard"]


def test_awaiting_human_without_question_offers_no_retry_discard(home):
    seed_ticket(home, "T-3", "idle", phase="awaiting-human")
    with _client(home) as c:
        assert "Retry" not in c.get("/").text and "Confirm discard" not in c.get("/").text
        assert "Retry" not in c.get("/t/T-3/head").text
        assert c.post("/t/T-3/retry", data={"ctx": "now"}).status_code == 409


def _proposal(home, key):
    p = home / "tickets" / key / "proposal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# P\n\n## Recommended\nDo A\n\n## Alternative 1\nDo B\n")


def test_proposal_tab_actions(home):
    key = "T-4"
    seed_ticket(home, key, "research", phase="awaiting-human",
                questions={f"research-approval-{key}": "Approve?\n   Recommended: Approve A"})
    _proposal(home, key)
    qid = f"research-approval-{key}"
    with _client(home) as c:
        tab = c.get(f"/t/{key}/proposal").text
        assert "Accept recommendation" in tab and "Pick alternative 1" in tab and "Needs more" in tab
        assert c.post(f"/t/{key}/proposal", data={"ctx": "proposal", "choice": "alternative", "n": "1"}).status_code == 409
        assert c.post(f"/t/{key}/proposal", data={"ctx": "proposal", "choice": "alternative", "n": "9", "confirm": "1"}).status_code == 409
        assert c.post(f"/t/{key}/proposal", data={"ctx": "proposal", "choice": "alternative", "n": "1", "confirm": "1"}).status_code == 200
        assert c.post(f"/t/{key}/proposal", data={"ctx": "proposal", "choice": "more", "text": "cost?"}).status_code == 200
        assert c.post(f"/t/{key}/proposal", data={"ctx": "proposal", "choice": "accept"}).status_code == 200
    assert _args(home, key) == [
        ("ans", {"text": "alternative 1", "qid": qid}),
        ("ans", {"text": "needs more: cost?", "qid": qid}),
        ("ans", {"text": "Approve A", "qid": qid, "accepted_recommendation": True}),
    ]


def test_accept_all_answers_only_recommended(home):
    seed_ticket(home, "T-1", "a", phase="awaiting-human", questions={"q1": REC})
    seed_ticket(home, "T-2", "b", phase="awaiting-human", questions={"q1": "Which colour?"})
    with _client(home) as c:
        page = c.get("/").text
        assert "Accept all recommendations" in page
        assert c.post("/now/accept-all", data={}).status_code == 409
        assert _args(home, "T-1") == []
        r = c.post("/now/accept-all", data={"confirm": "1"})
        assert r.status_code == 200
    assert _args(home, "T-1") == [("ans", {"text": "Yes, go ahead.", "qid": "q1", "accepted_recommendation": True})]
    assert _args(home, "T-2") == []
