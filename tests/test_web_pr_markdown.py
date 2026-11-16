"""T-284: the review brief and PR review threads render as sanitised Markdown in the web UI."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_review_ticket, seed_ticket  # noqa: E402
from maestro import event_log, snapshot as snap_mod  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

NASTY = ("<script>alert(1)</script> [x](javascript:alert(1)) ![alt](http://evil.test/i.png)")
FIELDS = ("summary", "checked", "decisions", "verdict", "reading_order")


def _client(home):
    return TestClient(create_app(home))


def _brief(home, key="T-60", **fields):
    payload = {"head_sha": "a" * 40, "path": "/x", **fields}
    event_log.append(home, key, "ReviewBriefWritten", payload, actor="r")
    snap_mod.rebuild(home, key)


def _assert_safe(html):
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "href" not in html.replace('href="/', "").replace('href="https://github.com', "")
    assert "<img" not in html and "evil.test" not in html


def test_brief_fields_render_markdown(home):
    seed_review_ticket(home, brief=False)
    _brief(home, **{f: f"**bold {f}**\n\n- item {f}\n\nuse `code_{f}`\n\n```\nfence_{f}\n```"
                    for f in FIELDS})
    with _client(home) as c:
        html = c.get("/t/T-60/review").text
    for f in FIELDS:
        assert f"<strong>bold {f}</strong>" in html and f"<li>item {f}</li>" in html
        assert f"<code>code_{f}</code>" in html and f"<pre><code>fence_{f}\n</code></pre>" in html
    panels = html.split("Findings")[0]
    assert "**" not in panels and "`" not in panels
    assert html.count('class="md"') >= 5 and "review-text" not in html


def test_brief_escapes_untrusted_markup(home):
    seed_review_ticket(home, brief=False)
    _brief(home, **{f: NASTY for f in FIELDS})
    with _client(home) as c:
        _assert_safe(c.get("/t/T-60/review").text)


def test_empty_and_missing_brief(home):
    seed_review_ticket(home, brief=False)
    _brief(home, summary="", checked="  ")
    with _client(home) as c:
        r = c.get("/t/T-60/review")
        assert r.status_code == 200 and "What the PR does" in r.text and "No brief yet" not in r.text
    seed_review_ticket(home, "T-61", brief=False)
    with _client(home) as c:
        assert "No brief yet" in c.get("/t/T-61/review").text


@pytest.fixture
def pr_home(home):
    seed_ticket(home, "T-9", "nine", phase="implementing", pr=9)
    return home


def _thread(home, body, reply=None):
    event_log.append(home, "T-9", "ReviewFeedbackReceived",
                     {"comment_id": "c1", "state": "COMMENTED", "author": "rev",
                      "body": body, "path": "a.py", "line": 3}, actor="t")
    if reply is not None:
        event_log.append(home, "T-9", "ReviewReplyPosted", {"comment_id": "c1", "body": reply},
                         actor="t")
    snap_mod.rebuild(home, "T-9")


def test_thread_and_reply_bodies_render_markdown(pr_home):
    _thread(pr_home, "**fix** this\n\n- one\n\n`x = 1`", reply="done in `abc`")
    with _client(pr_home) as c:
        html = c.get("/t/T-9/side").text
    threads = html.split('id="pr-threads"')[1].split('id="ci-timeline"')[0]
    assert '<p class="mono thread-head">rev [COMMENTED] a.py:3</p>' in threads
    assert "<strong>fix</strong>" in threads and "<li>one</li>" in threads
    assert "<code>x = 1</code>" in threads and "<code>abc</code>" in threads
    assert "**" not in threads and "`" not in threads
    # the reply sits inside the same thread container as its body
    block = threads.split('<div class="thread">')[1]
    assert "thread-body" in block and "thread-reply" in block
    assert threads.count('<div class="thread">') == 1
    assert 'class="md thread-body"' in threads and "review-text" not in threads


def test_thread_escapes_untrusted_markup(pr_home):
    _thread(pr_home, NASTY, reply=NASTY)
    with _client(pr_home) as c:
        threads = c.get("/t/T-9/side").text.split('id="pr-threads"')[1].split('id="ci-timeline"')[0]
    _assert_safe(threads)


def test_no_review_feedback(pr_home):
    with _client(pr_home) as c:
        assert "no review feedback" in c.get("/t/T-9/side").text


def test_md_class_not_pre_wrap():
    from pathlib import Path
    css = (Path(__file__).parent.parent / "maestro/web/static/maestro.css").read_text()
    assert ".md " in css or ".md{" in css
    for line in css.splitlines():
        if line.startswith((".thread-body", ".thread-reply", ".md ")):
            assert "pre-wrap" not in line
