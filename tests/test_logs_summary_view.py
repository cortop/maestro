"""T-234: past sessions collapse to digest summaries in the shared logs view (view-model, web, TUI)."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from conftest import seed_ticket
from tui_support import _make_app

from maestro import cli
from maestro.tui import LogsScreen
from maestro.view import logs as logs_view

KEY = "T-960"
SECRET = "UNIQUE-STEP-COMMAND-xyz"


def _log(home, epoch, cmd):
    d = home / "agent-logs" / KEY
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"reconcile-{KEY}-{epoch}.stream.jsonl"
    recs = [{"type": "system", "subtype": "init", "model": "m-x"},
            {"type": "assistant", "message": {"id": "m0", "role": "assistant", "content": [
                {"type": "tool_use", "id": "t0", "name": "Bash",
                 "input": {"command": cmd, "description": cmd}}]}},
            {"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 10}]
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    os.utime(p, (float(epoch) + 30, float(epoch) + 30))
    return p


def _setup(home):
    cli.main(["--home", str(home), "init"])
    seed_ticket(home, KEY, "Synthetic", phase="implementing")
    base = time.time() - 10 * 86400
    old = _log(home, f"{base:.1f}", SECRET)
    new = _log(home, f"{base + 1000:.1f}", "ls")
    return old, new


def test_past_sessions_are_summaries_and_newest_is_live(home):
    old, new = _setup(home)
    tl = logs_view.build(home, KEY)
    assert tl.live["path"] == str(new)
    assert [p.sess["path"] for p in tl.past] == [str(old)]
    assert SECRET not in tl.past[0].summary and "1 calls" in tl.past[0].summary


def test_pruned_session_keeps_its_summary(home):
    old, _ = _setup(home)
    (home / "config.toml").write_text(
        "[maestro]\nsession_log_retention_days = 5\nsession_log_max_per_ticket = 0\n", encoding="utf-8")
    assert cli.main(["--home", str(home), "prune-logs", KEY]) == 0
    assert not old.exists()
    tl = logs_view.build(home, KEY)
    pruned = [p for p in tl.past if p.pruned]
    assert len(pruned) == 2 and all("1 calls" in p.summary for p in pruned)


def test_no_sessions_no_past_section(home):
    cli.main(["--home", str(home), "init"])
    seed_ticket(home, KEY, "Synthetic", phase="implementing")
    tl = logs_view.build(home, KEY)
    assert tl.live is None and tl.past == ()


def test_web_logs_partial_collapsed_details(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.web.app import create_app
    _setup(home)
    with TestClient(create_app(home)) as c:
        r = c.get(f"/t/{KEY}/logs")
    assert r.status_code == 200
    assert r.text.count("<details class=\"past-session\"") == 1
    assert "<details class=\"past-session\" open" not in r.text


def test_web_logs_partial_empty(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.web.app import create_app
    cli.main(["--home", str(home), "init"])
    seed_ticket(home, KEY, "Synthetic", phase="implementing")
    with TestClient(create_app(home)) as c:
        r = c.get(f"/t/{KEY}/logs")
    assert r.status_code == 200 and "Past sessions" not in r.text


def _pane(app):
    from textual.widgets import RichLog
    return "\n".join(s.text for s in app.screen.query_one("#logs-view", RichLog).lines)


def test_tui_logs_collapse_past_and_expand_on_demand(home):
    _setup(home)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = KEY
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = _pane(app)
            assert "▸" in pane and SECRET not in pane  # past session: summary row only
            screen = app.screen
            old = logs_view.build(home, KEY).past[0].sess
            screen.query_one("#logs-view").clear()
            screen._start_tail(pick=old)  # what the session picker does on select
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert SECRET in _pane(app)  # expanded: full step detail
            await pilot.press("c")
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = _pane(app)
            assert "▸" in pane and SECRET not in pane
            assert app._exception is None

    asyncio.run(_inner())
