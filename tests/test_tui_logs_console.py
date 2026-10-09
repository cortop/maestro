"""Mounted-app tests for the LogsScreen live console (T-171)."""
from __future__ import annotations

import asyncio
import subprocess
import sys

from textual.widgets import Static

from maestro import claims, store
from maestro.tui import LogsScreen
from tui_support import _make_app


# T-171: LogsScreen live console                                              #
# --------------------------------------------------------------------------- #

def _console_stream(path, *, extra=()):
    import json as _json
    recs = [{"type": "system", "subtype": "init", "model": "m-x"}]
    for out in (1, 2, 5):  # one message split over three records
        recs.append({"type": "assistant", "message": {
            "id": "m1", "usage": {"input_tokens": 10, "output_tokens": out},
            "content": [{"type": "text", "text": f"said-{path.name.split('-')[3][:4]}"}]}})
    recs += list(extra)
    recs += [{"type": "result", "subtype": "success", "total_cost_usd": 0.40, "num_turns": 22},
             {"type": "result", "subtype": "success", "total_cost_usd": 0.84, "num_turns": 22,
              "duration_ms": 1000}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(r) + "\n" for r in recs), encoding="utf-8")


async def _open_logs(app, pilot):
    await pilot.pause()
    app._selected_key = "T-3"
    await app.run_action("view_logs")
    await pilot.pause()
    assert isinstance(app.screen_stack[-1], LogsScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()


def _pane(app):
    from textual.widgets import RichLog
    return "\n".join(s.text for s in app.screen.query_one("#logs-view", RichLog).lines)


def test_logs_console_header_shows_cost_and_turns(seeded_home):
    _console_stream(seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            hdr = str(app.screen.query_one("#logs-summary", Static).render())
            assert "$0.84" in hdr and "22 turns" in hdr
            assert app._exception is None

    asyncio.run(_inner())


def test_logs_console_follow_toggle(seeded_home):
    _console_stream(seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl")

    async def _inner():
        from textual.widgets import RichLog
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            idx = app._filter_idx
            log = app.screen.query_one("#logs-view", RichLog)
            await pilot.press("f")
            await pilot.pause()
            assert log.auto_scroll is False
            assert "paused" in str(app.screen.query_one("#logs-summary", Static).render())
            await pilot.press("f")
            await pilot.pause()
            assert log.auto_scroll is True
            assert "paused" not in str(app.screen.query_one("#logs-summary", Static).render())
            assert app._filter_idx == idx and app._exception is None

    asyncio.run(_inner())


def test_logs_console_session_picker(seeded_home):
    d = seeded_home / "agent-logs" / "T-3"
    _console_stream(d / "reconcile-T-3-1000.000000.stream.jsonl")
    _console_stream(d / "reconcile-T-3-2000.000000.stream.jsonl")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            assert "said-1000" in _pane(app)
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("down", "enter")  # newest first -> second is the older
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = _pane(app)
            assert "reconcile-T-3-1000.000000" in pane
            assert "reconcile-T-3-2000.000000" not in pane
            running = [w for w in app.workers if w.name == "tail-logs" and w.is_running]
            assert len(running) <= 1
            assert app._exception is None

    asyncio.run(_inner())


def test_logs_console_auto_advances_on_claim_swap(seeded_home):
    d = seeded_home / "agent-logs" / "T-3"
    a, b = d / "reconcile-T-3-1000.000000.stream.jsonl", d / "reconcile-T-3-2000.000000.stream.jsonl"
    _console_stream(a)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    p1, p2 = subprocess.Popen(sleeper), subprocess.Popen(sleeper)

    def _claim(proc, path):
        store.write_json(claims.claim_path(seeded_home, "T-3"),
                         {"pid": proc.pid, "name": "reconcile-T-3", "ts": store.iso_now(),
                          "epoch": store.now_epoch(), "log_path": str(path)})

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            _claim(p1, a)
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause(1.0)
            p1.kill()
            p1.wait()
            _console_stream(b)
            _claim(p2, b)
            for _ in range(40):
                await pilot.pause(0.25)
                if "said-2000" in _pane(app):
                    break
            pane = _pane(app)
            assert "── next session" in pane
            assert pane.index("── next session") < pane.rindex("said-2000")
            assert claims.claim_path(seeded_home, "T-3").exists()
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()

    try:
        asyncio.run(_inner())
    finally:
        for p in (p1, p2):
            p.kill()
            p.wait(timeout=5)


def test_logs_console_jump_to_next_error(seeded_home):
    import json as _json
    from textual.widgets import RichLog
    path = seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-3000.000000.stream.jsonl"
    recs = []
    for i in range(80):
        recs.append({"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "text", "text": f"filler-{i}"}]}})
        if i in (30, 60):
            recs.append({"type": "result", "subtype": "success", "is_error": True, "result": "boom"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 20)) as pilot:
            await _open_logs(app, pilot)
            log = app.screen.query_one("#logs-view", RichLog)
            assert len(app.screen._err_lines) == 2
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            first = log.scroll_y
            assert first == app.screen._err_lines[0]
            await pilot.press("e")
            await pilot.pause()
            assert log.scroll_y == app.screen._err_lines[1] > first
            assert app._exception is None

    asyncio.run(_inner())
