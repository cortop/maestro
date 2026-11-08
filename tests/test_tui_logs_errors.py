"""Mounted-app tests: LogsScreen shows tool errors, pi read paths, and a worker-loaded picker (T-224)."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import RichLog

from maestro import steplog
from maestro.tui.events import render_log_line, render_pi_log_line
from tui_support import _make_app


def _write(path, recs, raw_extra=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, separators=(",", ":")) for r in recs] + list(raw_extra)
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")


def _claude_recs(n_filler=60):
    recs = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"filler {i}"} for i in range(n_filler)]}}]
    recs.append({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "false"}}]}})
    recs.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
         "content": "boom: it failed\nsecond line"}]}})
    recs += [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"after {i}"} for i in range(60)]}}]
    return recs


def _pi_recs():
    return [
        {"type": "tool_execution_start", "toolName": "read", "toolCallId": "c1",
         "args": {"path": "src/a.py"}},
        {"type": "tool_execution_end", "toolName": "read", "toolCallId": "c1", "isError": True,
         "result": {"content": [{"type": "text", "text": "ENOENT: no such file\nmore"}]}},
        {"type": "tool_execution_end", "toolName": "bash", "toolCallId": "c2", "isError": False,
         "result": {"content": [{"type": "text", "text": "fine"}]}},
    ]


async def _open_logs(app, pilot):
    await pilot.pause()
    app._selected_key = "T-3"
    await app.run_action("view_logs")
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _pane(app):
    return "\n".join(s.text for s in app.screen.query_one("#logs-view", RichLog).lines)


def test_claude_tool_result_error_renders_red():
    names: dict = {}
    render_log_line(_claude_recs(0)[1], names)
    out = render_log_line(_claude_recs(0)[2], names)
    assert out == ["[red]✗ Bash: boom: it failed[/red]"]
    ok = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    assert render_log_line(ok) == []


def test_pi_error_end_renders_red_and_ok_end_silent():
    recs = _pi_recs()
    assert render_pi_log_line(recs[1]) == ["[red]✗ read: ENOENT: no such file[/red]"]
    assert render_pi_log_line(recs[2]) == []


def test_pi_read_grep_find_summaries_show_paths():
    assert steplog.pi_summary("read", {"path": "src/a.py"}) == "src/a.py"
    assert steplog.pi_summary("grep", {"pattern": "foo", "path": "src"}) == "foo src"
    assert steplog.pi_summary("find", {"pattern": "*.py", "path": "tests"}) == "*.py tests"
    assert "src/a.py" in render_pi_log_line(_pi_recs()[0])[0]


def test_tool_name_with_markup_does_not_raise():
    pi = render_pi_log_line({"type": "tool_execution_start", "toolName": "[/x]", "args": {}})
    cl = render_log_line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "[/x]", "input": {}}]}})
    err = render_pi_log_line({"type": "tool_execution_end", "toolName": "[/x]", "isError": True,
                             "result": {"content": [{"type": "text", "text": "[/y] bad"}]}})
    from rich.text import Text
    for line in pi + cl + err:
        Text.from_markup(line)


def test_logs_screen_error_jump_claude_and_pi(seeded_home):
    d = seeded_home / "agent-logs" / "T-3"
    _write(d / "reconcile-T-3-1000.000000.stream.jsonl", _claude_recs())
    _write(d / "reconcile-T-3-2000.000000.pi.jsonl", _pi_recs(),
           raw_extra=['{"type":"message_update","x":"noise"}'])

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            pane = _pane(app)
            # T-234: the older claude session is a summary row; the live pi session has detail
            assert "✗ Bash: boom: it failed" not in pane
            assert "✗ read: ENOENT: no such file" in pane
            assert "noise" not in pane
            screen = app.screen
            assert len(screen._err_lines) == 1
            log = screen.query_one("#logs-view", RichLog)
            await pilot.press("f")  # pause follow, then start from the top
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert screen._last_jump == screen._err_lines[0]
            from maestro.sessions import list_sessions
            claude = next(s for s in list_sessions(seeded_home, "T-3") if s["format"] == "stream-json")
            log.clear()
            screen._err_lines = []
            screen._last_jump = -1
            screen._start_tail(pick=claude)  # expand the past session on demand
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "✗ Bash: boom: it failed" in _pane(app) and len(screen._err_lines) == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_session_picker_loads_in_worker_thread(seeded_home, monkeypatch):
    import threading

    from maestro.tui import screens
    d = seeded_home / "agent-logs" / "T-3"
    _write(d / "reconcile-T-3-1000.000000.stream.jsonl", _claude_recs(1))
    seen: list[bool] = []
    real = screens.list_sessions

    def spy(*a, **kw):
        if kw.get("with_outcome"):
            seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)

    monkeypatch.setattr(screens, "list_sessions", spy)

    async def _inner():
        from maestro.tui.modals import _SessionPickModal
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_logs(app, pilot)
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _SessionPickModal)
            assert seen == [False]
            assert app._exception is None

    asyncio.run(_inner())
