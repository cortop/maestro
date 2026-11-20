"""view.reviews: groups, history window, counts without logs, sentences, detail timeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from maestro import event_log, store
from maestro import snapshot as snap_mod
from maestro.config import Config
from maestro.view import reviews as rv
from maestro.view import ticket as ticket_view

SHA = "a" * 40
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()


def seed(home, key, phase, *, questions=None, brief=False, pending=False, request=True,
         pr=True, reason="", number=7):
    store.atomic_write(store.spec_path(home, key), f"# {key}\n")
    event_log.append(home, key, "TicketCreated", {
        "title": key, "kind": "review", "repo": "widgets",
        "external_id": f"acme/widgets#{number}", "external_source": "github-pr"}, actor="d")
    if request:
        event_log.append(home, key, "ReviewRequested", {
            "via": "slack", "requested_ts": "2026-05-31T10:00:00+00:00",
            "requester": {"name": "Ada", "id": "U1"},
            "where": {"channel": "reviews", "permalink": "https://x/p1"},
            "pr": {}}, actor="d")
    if pr:
        event_log.append(home, key, "ReviewPrObserved", {
            "title": "Fix parser", "author": "bob", "state": "open", "draft": False,
            "head_sha": SHA, "additions": 10, "deletions": 2, "changed_files": 3}, actor="d")
    event_log.append(home, key, "ReviewRoundStarted",
                     {"round": 1, "head_sha": SHA, "url": f"https://github.com/acme/widgets/pull/{number}"},
                     actor="h")
    event_log.append(home, key, "TestRunCaptured", {"tree_key": "t", "command": "x",
                                                    "exit_code": 0, "passed": True}, actor="d")
    event_log.append(home, key, "ReviewFindingRecorded", {
        "head_sha": SHA, "file": "a.py", "line": 1, "severity": "nit", "comment": "c"}, actor="r")
    if brief:
        event_log.append(home, key, "ReviewBriefWritten",
                         {"head_sha": SHA, "summary": "s", "verdict": "approve"}, actor="r")
    if pending:
        event_log.append(home, key, "ReviewPendingCreated",
                         {"review_id": 1, "pr_number": number, "head_sha": SHA}, actor="r")
    for qid in questions or []:
        event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": "?"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": reason}, actor="r")
    return snap_mod.rebuild(home, key)


def age(home, key, days):
    """Stamp a closed review's snapshot as updated *days* before NOW."""
    path = store.snapshot_path(home, key)
    d = store.read_json(path)
    d["updated_ts"] = datetime.fromtimestamp(NOW - days * 86400, timezone.utc).isoformat()
    store.atomic_write(path, __import__("json").dumps(d))


def seed_all(home):
    seed(home, "r-need", "awaiting-human", questions=["review-brief-r-need-x"], brief=True)
    seed(home, "r-prog", "qa")
    seed(home, "r-gh", "awaiting-ci", brief=True, pending=True)
    seed(home, "r-done", "done", brief=True, reason="review submitted")
    age(home, "r-done", 1)


def test_build_groups_and_row_fields(home):
    seed_all(home)
    v = rv.build(home, Config(home=home), NOW)
    assert [r.key for r in v.needs_you] == ["r-need"]
    assert [r.key for r in v.in_progress] == ["r-prog"]
    assert [r.key for r in v.on_github] == ["r-gh"]
    assert [r.key for r in v.history] == ["r-done"]
    assert v.counts == {"needs_you": 1, "in_progress": 1, "on_github": 1, "history": 1}
    row = v.needs_you[0]
    assert (row.title, row.requested_by, row.where_label) == ("Fix parser", "Ada", "Slack #reviews")
    assert row.status == "brief-ready" and row.requested_at == "2026-05-31T10:00:00+00:00"
    assert row.size == {"additions": 10, "deletions": 2, "changed_files": 3}
    assert row.findings == {"blocking": 0, "question": 0, "nit": 1} and row.verdict == "approve"
    assert v.history[0].outcome == "submitted" and v.history[0].closed_at
    assert "last_poll" in v.slack


def test_history_window_order_and_cap(home):
    for i in range(25):
        key = f"r-{i:02d}"
        seed(home, key, "done", reason="review submitted", number=100 + i)
        age(home, key, 1 + i * 0.1)
    seed(home, "r-old", "done", number=500)
    age(home, "r-old", 30)
    hist = rv.build(home, Config(home=home), NOW).history
    assert len(hist) == rv.HISTORY_LIMIT
    assert [r.key for r in hist] == [f"r-{i:02d}" for i in range(20)]
    wide = rv.build(home, Config(home=home), NOW, history_days=60).history
    assert "r-old" not in [r.key for r in wide] and len(wide) == 20


def test_counts_equal_build_and_read_no_log(home, monkeypatch):
    seed_all(home)
    seed(home, "r-old", "done", number=500)
    age(home, "r-old", 30)
    expected = rv.build(home, Config(home=home), NOW).counts

    def boom(*a, **k):
        raise AssertionError("event log read")
    monkeypatch.setattr(event_log, "read", boom)
    assert rv.counts(home, NOW) == expected == {
        "needs_you": 1, "in_progress": 1, "on_github": 1, "history": 1}


def test_status_sentences(home):
    gh = seed(home, "r-gh", "awaiting-ci", brief=True, pending=True)
    assert "waiting on GitHub" in rv.status_sentence(gh)
    stranded = seed(home, "r-st", "ready", brief=True, number=8)
    assert "brief is ready" in rv.status_sentence(stranded)
    assert rv.build(home, Config(home=home), NOW).needs_you[0].key == "r-st"


def test_ticket_status_delegates_and_keeps_strings(home):
    s = seed(home, "r-q", "qa")
    assert ticket_view._review_status(s) == "An agent is checking the pull request."
    assert ticket_view.REVIEW_RAIL == rv.RAIL


def test_no_recorded_request_is_inferred(home):
    seed(home, "r-nr", "ready", request=False, pr=False)
    row = rv.build(home, Config(home=home), NOW).in_progress[0]
    assert row.requested_by is None and row.requested_at_inferred
    assert row.title == "acme/widgets#7" and row.size is None


def test_detail_timeline_through_brief_ready(home):
    seed(home, "r-d", "awaiting-human", questions=["review-brief-r-d-x"], brief=True)
    d = rv.detail(home, Config(home=home), "r-d", NOW)
    assert [t["label"] for t in d.timeline] == [
        "filed", "asked", "round started", "tests done", "brief ready"]
    assert all(t["ts"] for t in d.timeline)
    assert d.brief["verdict"] == "approve" and len(d.findings) == 1 and len(d.requests) == 1
    assert rv.detail(home, Config(home=home), "nope", NOW) is None
