"""T-264: the web Sessions tab reads live and past sessions step by step."""
from __future__ import annotations

import json
import os
import re
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import _write_stream_log, seed_phase  # noqa: E402
from maestro import claims, cli, config as config_mod, ops, sessions  # noqa: E402
from maestro.view import logs as logs_view  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def _text(i, text):
    return {"type": "assistant", "message": {"id": f"m{i}", "role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def _tool(i):
    return {"type": "assistant", "message": {"id": f"t{i}", "role": "assistant", "content": [
        {"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": f"echo {i}"}}]}}


def _fail():
    return {"type": "result", "subtype": "error_during_execution", "is_error": True,
            "duration_ms": 5, "result": "boom"}


def _append(path, rec):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _ids(html):
    return [int(i) for i in re.findall(r'<li class="step" data-index="(\d+)"', html)]


@pytest.fixture
def board(home):
    seed_phase(home, "T-1")
    return home


def _client(home):
    return TestClient(create_app(home))


def _claim(home, path):
    claims.write_claim(home, "T-1", os.getpid(), "t", log_path=str(path))


def test_live_steps_show_without_any_past_session(board):
    log = _write_stream_log(board, "T-1", time.time(), [_text(1, "Step: reading the spec"), _tool(2)])
    _claim(board, log)
    with _client(board) as c:
        html = c.get("/t/T-1/logs").text
    assert 'id="live-transcript"' in html
    assert "Step: reading the spec" in html and "[tool_use:Bash]" in html
    assert "Past sessions" not in html


def test_no_sessions_says_so(board):
    with _client(board) as c:
        assert "No sessions yet" in c.get("/t/T-1/logs").text


def test_new_steps_arrive_without_reload_and_follow_toggle_exists(board):
    log = _write_stream_log(board, "T-1", time.time(), [_text(1, "first")])
    _claim(board, log)
    sid = sessions.list_sessions(board, "T-1")[0]["session_id"]
    with _client(board) as c:
        html = c.get("/t/T-1/logs").text
        assert 'class="follow"' in html and "next-error" in html
        m = re.search(r'class="tail" hx-get="([^"]+)" hx-trigger="sse:ticket:T-1, every 3s"', html)
        assert m, "the live transcript polls for new steps on the ticket's stream event"
        assert m.group(1).endswith(f"/logs/{sid}/steps?after=1")
        assert c.get(m.group(1)).text.count('class="step"') == 0
        _append(log, _text(2, "second"))
        _append(log, _tool(3))
        new = c.get(m.group(1)).text
    assert _ids(new) == [1, 2] and "second" in new
    assert 'after=3' in new  # the replacement tail resumes after the newest step


def test_any_session_opens_as_the_transcript_maestro_logs_prints(board, capsys):
    recs = [_text(1, "alpha"), _tool(2), _text(3, "line one\nline two"), _fail()]
    old = _write_stream_log(board, "T-1", time.time() - 500, recs)
    new = _write_stream_log(board, "T-1", time.time(), [_text(9, "live")])
    _claim(board, new)
    sid = sessions.list_sessions(board, "T-1")[1]["session_id"]
    assert old.stem.startswith(sid)
    assert cli.main(["--home", str(board), "logs", "T-1", "--session", sid]) == 0
    printed = capsys.readouterr().out.rstrip("\n").split("\n")
    with _client(board) as c:
        html = c.get(f"/t/T-1/logs/{sid}").text
    from html import unescape
    items = re.findall(r'<li class="step"[^>]*>(?:<span class="verdict v-error">error</span> )?(.*?)</li>', html, re.S)
    shown = "\n".join(unescape(i) for i in items).split("\n")
    assert shown == printed
    assert 'data-live' not in html and 'class="follow"' not in html  # a past session does not follow


def test_error_steps_are_marked_and_next_error_control_is_offered(board):
    recs = [_text(1, "ok"), _fail(), _text(2, "after"),
            {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "five_hour", "status": "rejected"}}]
    _write_stream_log(board, "T-1", time.time(), recs)
    sid = sessions.list_sessions(board, "T-1")[0]["session_id"]
    with _client(board) as c:
        html = c.get(f"/t/T-1/logs/{sid}").text
    assert len(re.findall(r'data-error="1"', html)) == 2
    assert html.count('<span class="verdict v-error">error</span>') == 2
    assert 'class="btn btn-ghost btn-sm next-error"' in html
    js = (config_mod.__file__ and open(os.path.join(os.path.dirname(config_mod.__file__),
                                                    "web/static/session-log.js")).read())
    assert "[data-error]" in js and "errorCursor" in js
    with _client(board) as c:
        assert "/static/session-log.js" in c.get("/t/T-1").text


def test_long_log_loads_newest_first_then_earlier_pages_without_gaps(board):
    n = logs_view.PAGE_STEPS * 2 + 30
    _write_stream_log(board, "T-1", time.time(), [_text(i, f"s{i}") for i in range(n)])
    sid = sessions.list_sessions(board, "T-1")[0]["session_id"]
    with _client(board) as c:
        html = c.get(f"/t/T-1/logs/{sid}").text
        seen = _ids(html)
        assert seen == list(range(n - logs_view.PAGE_STEPS, n))
        while (m := re.search(r'class="load-earlier"><a href="#" hx-get="([^"]+)"', html)):
            html = c.get(m.group(1)).text
            assert len(_ids(html)) <= logs_view.PAGE_STEPS
            seen = _ids(html) + seen
    assert seen == list(range(n))


def test_pruned_session_shows_digest_and_says_log_is_gone(board):
    log = _write_stream_log(board, "T-1", time.time() - 1000, [_text(1, "x"), _tool(2)])
    cfg = config_mod.load(str(board))
    sess = sessions.list_sessions(board, "T-1")[0]
    ops._write_digest_sidecar(cfg, "T-1", sess, [sess])
    log.unlink()
    with _client(board) as c:
        listing = c.get("/t/T-1/logs").text
        page = c.get(f"/t/T-1/logs/{sess['session_id']}").text
        gone = c.get("/t/T-1/logs/reconcile-T-1-1.000000")
        steps = c.get(f"/t/T-1/logs/{sess['session_id']}/steps?before=1")
    assert "raw log of this session is gone" in listing and "open-transcript" not in listing
    assert "raw log of this session is gone" in page and 'class="step"' not in page
    assert gone.status_code == 404 and steps.status_code == 404


def test_transcript_is_read_only_and_works_under_read_only(board):
    log = _write_stream_log(board, "T-1", time.time(), [_text(1, "hi")])
    _claim(board, log)
    sid = sessions.list_sessions(board, "T-1")[0]["session_id"]
    with TestClient(create_app(board, read_only=True)) as c:
        assert "hi" in c.get(f"/t/T-1/logs/{sid}").text
        assert "hi" in c.get("/t/T-1/logs").text
        assert c.post(f"/t/T-1/logs/{sid}").status_code in (404, 405)
