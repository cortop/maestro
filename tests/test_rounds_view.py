"""`maestro logs KEY --summary` and the rounds view-model, over a synthetic ticket."""
from __future__ import annotations

import json
import os

from maestro import cli, event_log, snapshot as snap_mod, store
from maestro.view import rounds as rounds_view

KEY = "T-900"
T0 = 1_700_000_000.0  # fixed epoch; every event time is an offset from it
DAY = 86400


class Clock:
    def __init__(self, monkeypatch):
        self.t = T0
        monkeypatch.setattr(store, "iso_now", lambda: store.epoch_to_iso(self.t))

    def add(self, home, typ, payload=None, after=0):
        self.t += after
        ev = event_log.append(home, KEY, typ, payload or {}, actor="t")
        snap_mod.rebuild(home, KEY)
        return ev


def _spec(home, acs=2):
    body = "".join(f"- [ ] criterion {i}\n" for i in range(1, acs + 1))
    store.atomic_write(store.spec_path(home, KEY), f"# {KEY}\n\n## Acceptance criteria\n{body}")


def _story(home, monkeypatch):
    _spec(home)
    c = Clock(monkeypatch)
    c.add(home, "TicketCreated", {"title": "Synthetic widget", "source": "test", "spec_hash": "x"})
    c.add(home, "PhaseChanged", {"phase": "ready"}, 60)
    c.add(home, "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, 60)
    c.add(home, "PhaseChanged", {"phase": "qa", "reason": "implementing round done"}, 600)
    c.add(home, "AcQaVerdict", {"ac_hash": "h1", "ac_index": 1, "verdict": "pass", "evidence": "ok"}, 60)
    c.add(home, "AcQaVerdict", {"ac_hash": "h2", "ac_index": 2, "verdict": "fail",
                                "evidence": "grid kept\nsecond line"}, 60)
    c.add(home, "Note", {"text": "needs a table\nmore"}, 1)
    c.add(home, "PhaseChanged", {"phase": "implementing",
                                 "reason": "QA failed AC 2: grid kept"}, 60)
    c.add(home, "PhaseChanged", {"phase": "qa", "reason": "qa-fix round done: Laid out the table"}, 300)
    c.add(home, "AcQaVerdict", {"ac_hash": "h1", "ac_index": 1, "verdict": "pass", "evidence": "ok"}, 30)
    c.add(home, "AcQaVerdict", {"ac_hash": "h2", "ac_index": 2, "verdict": "pass", "evidence": "ok"}, 30)
    c.add(home, "PhaseChanged", {"phase": "awaiting-ci", "reason": "qa passed"}, 60)
    c.add(home, "PrOpened", {"number": 5, "url": "https://example.com/pr/5", "draft": True})
    for cid, author, state in (("c1", "alice", "COMMENTED"), ("c2", "bob", "COMMENTED"),
                               ("c3", "ci-helper[bot]", "COMMENTED"), ("c4", "carol", "APPROVED")):
        c.add(home, "ReviewFeedbackReceived", {"comment_id": cid, "author": author, "state": state,
                                               "body": "![P1](https://x/y.png) rename this"}, 10)
    c.add(home, "PhaseChanged", {"phase": "implementing",
                                 "reason": "changes requested: 2 comments"}, 120)
    c.add(home, "ReviewReplyPosted", {"comment_id": "c1", "kind": "inline", "body": "done"}, 60)
    c.add(home, "PhaseChanged", {"phase": "awaiting-ci", "reason": "review-feedback round done"}, 60)
    c.add(home, "Failed", {"error": "watchdog boom"}, 10)
    c.add(home, "Stalled", {"reason": "no progress"}, 10)
    return c


def test_summary_cli_without_session_logs(home, monkeypatch, capsys):
    _story(home, monkeypatch)
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    assert cli.main(["logs", KEY, "--summary"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith(f"{KEY} · degraded · Synthetic widget")
    assert any(l.startswith("Time ") for l in lines) and any(l.startswith("Trouble") for l in lines)
    assert "No session logs" not in out
    # QA chain: fail -> qa-fix -> pass per AC
    assert "QA       ✗ AC2 → qa-fix → ✓ 2/2" in out
    # QA rows
    assert "✗ AC 2: grid kept" in out and "note: needs a table" in out
    assert "✓ 2/2" in out
    # implementing rows labelled by round; non-bare closing reason quoted, bare one not
    assert '"Laid out the table"' in out
    fresh = next(l for l in lines if " fresh " in l)
    assert '"' not in fresh
    # review-feedback row
    assert "2 comments → 1 reply" in out
    # failed / stalled lines
    assert any("⚠ Failed" in l and "watchdog boom" in l for l in lines)
    assert any("⚠ Stalled" in l and "no progress" in l for l in lines)


def test_pr_line_excludes_bots_and_approvals(home, monkeypatch):
    _story(home, monkeypatch)
    tr = rounds_view.build(home, KEY, now=T0 + DAY)
    assert tr.pr == "2 comments · 1 replied · 1 without reply · 2 bot/system"
    assert "2 failures" not in tr.trouble and "1 stalled" in tr.trouble and "1 failure" in tr.trouble


def test_json_view_model_and_completion(home, monkeypatch, capsys):
    _story(home, monkeypatch)
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    assert cli.main(["logs", KEY, "--summary", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["key"] == KEY and data["rows"] and data["qa"].startswith("✗ AC2")
    from pathlib import Path
    comp = Path(cli.__file__).parent / "_assets" / "completions" / "_maestro"
    assert "--summary" in comp.read_text(encoding="utf-8")


def test_awaiting_human_row_answered_and_unanswered(home, monkeypatch):
    _spec(home)
    c = Clock(monkeypatch)
    c.add(home, "TicketCreated", {"title": "Q", "source": "test", "spec_hash": "x"})
    c.add(home, "PhaseChanged", {"phase": "awaiting-human"}, 10)
    c.add(home, "QuestionAsked", {"qid": "q1", "text": "Which table?"}, 1)
    c.add(home, "QuestionAnswered", {"qid": "q1", "answer": "the big one"}, 3600)
    c.add(home, "PhaseChanged", {"phase": "ready"}, 1)
    c.add(home, "PhaseChanged", {"phase": "awaiting-human"}, 10)
    c.add(home, "QuestionAsked", {"qid": "q2", "text": "Which chair?"}, 1)
    tr = rounds_view.build(home, KEY, now=c.t + 60)
    asked = [r for r in tr.rows if r.label == "asked you"]
    assert asked[0].outcome == '"Which table?" · answered after 1h00m'
    assert asked[1].outcome == '"Which chair?" · unanswered'
    assert "1 question unanswered" in tr.trouble


def test_identical_rows_collapse_but_warnings_do_not(home, monkeypatch):
    _spec(home)
    c = Clock(monkeypatch)
    c.add(home, "TicketCreated", {"title": "C", "source": "test", "spec_hash": "x"})
    c.add(home, "PhaseChanged", {"phase": "ready"}, 1)
    c.add(home, "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, 1)
    for _ in range(3):
        c.add(home, "PhaseChanged", {"phase": "awaiting-ci"}, 1)
        c.add(home, "PhaseChanged", {"phase": "implementing",
                                     "reason": "CI failing: lint"}, 1)
    c.add(home, "Failed", {"error": "x"}, 1)
    c.add(home, "Failed", {"error": "x"}, 1)
    tr = rounds_view.build(home, KEY, now=c.t + 5)
    ci = [r for r in tr.rows if r.label == "ci-fix"]
    assert len(ci) == 1 and ci[0].count == 3
    assert "ci-fix ×3" in "\n".join(rounds_view.format_lines(tr))
    assert len([r for r in tr.rows if r.warn]) == 2


def test_sessions_attributed_by_epoch_window_and_pruned(home, monkeypatch):
    _spec(home)
    c = Clock(monkeypatch)
    c.add(home, "TicketCreated", {"title": "S", "source": "test", "spec_hash": "x"})
    c.add(home, "PhaseChanged", {"phase": "ready"}, 1)
    c.add(home, "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, 1)
    impl_start = c.t
    c.add(home, "PhaseChanged", {"phase": "qa", "reason": "implementing round done"}, 1800)
    qa_start = c.t
    c.add(home, "PhaseChanged", {"phase": "implementing", "reason": "QA failed AC 1: x"}, 600)
    logs = home / "agent-logs" / KEY
    logs.mkdir(parents=True)
    f = logs / f"reconcile-{KEY}-{int(impl_start) + 5}.5.log"
    f.write_text("x")
    os.utime(f, (impl_start + 905.5, impl_start + 905.5))  # busy 900s = 15m inside the impl window
    now = c.t + 100
    tr = rounds_view.build(home, KEY, now=now)
    fresh = next(r for r in tr.rows if r.label == "fresh")
    assert fresh.duration == "15m" and "pruned" not in fresh.outcome
    qa = next(r for r in tr.rows if r.label == "QA")
    assert qa.duration == "" and "pruned" not in qa.outcome  # recent: logs not expected pruned
    # far in the future the log-less rounds read as pruned, the attributed one does not
    old = rounds_view.build(home, KEY, now=now + 30 * DAY)
    assert "(session log pruned)" in next(r for r in old.rows if r.label == "QA").outcome
    assert "pruned" not in next(r for r in old.rows if r.label == "fresh").outcome
    assert qa_start > impl_start


def test_review_skill_three_states(home, monkeypatch):
    _spec(home)
    c = Clock(monkeypatch)
    c.add(home, "TicketCreated", {"title": "R", "source": "test", "spec_hash": "x"})
    base = "[maestro]\n"
    (home / "config.toml").write_text(base, encoding="utf-8")
    assert "qa_skill not configured" in rounds_view.build(home, KEY, now=c.t).review
    pi_cfg = base.replace("[maestro]\n", '[maestro]\nqa_skill = "/review-widget"\nrunner = "pi"\n') + \
        '\n[runner.pi]\nphases = ["implementing", "qa"]\n'
    (home / "config.toml").write_text(pi_cfg, encoding="utf-8")
    line = rounds_view.build(home, KEY, now=c.t).review
    assert "NOT RUN" in line and "/review-widget" in line and "pi cannot run skills" in line
    (home / "config.toml").write_text('[maestro]\nqa_skill = "/review-widget"\n', encoding="utf-8")
    assert rounds_view.build(home, KEY, now=c.t).review == "skill /review-widget (current config)"


def test_review_threads_keep_image_alt_and_replies():
    events = [
        {"type": "ReviewFeedbackReceived", "seq": 1,
         "payload": {"comment_id": "a", "author": "dave", "state": "COMMENTED", "path": "f.py",
                     "line": 3, "body": "![P2](https://x/i.png) tidy"}},
        {"type": "ReviewReplyPosted", "seq": 2, "payload": {"comment_id": "a", "body": "ok"}},
    ]
    blocks = rounds_view.review_threads(events)
    assert blocks == ["dave [COMMENTED] f.py:3\nP2 tidy\n  ↳ reply: ok"]


def test_review_screen_uses_shared_join():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "maestro" / "tui" / "screens.py").read_text(encoding="utf-8")
    assert "rounds_view.review_threads(" in src
    assert 'e["type"] == "ReviewReplyPosted"' not in src.split("def action_threads")[1].split("def action_ci_timeline")[0]
