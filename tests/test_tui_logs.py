"""Mounted-app tests for LogsScreen rendering."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from maestro import claims, store
from maestro.tui import LogsScreen
from tui_support import _make_app, _run_modal_test


def test_logs_screen_open_and_escape(seeded_home):
    """LogsScreen runs a thread worker that calls app.call_from_thread — the exact
    surface of the 'call_from_thread is on App, not Screen' regression."""
    _run_modal_test(seeded_home, "T-3", "view_logs", LogsScreen)


def test_logs_screen_stops_tail_on_denied_claim(seeded_home):
    """A claim whose recorded epoch predates a real, live, non-reconciler process
    (pid reuse) is verified-denied — the tail worker must stop instead of polling
    a genuinely-alive-but-wrong pid forever (T-17). Proved via the real app, not
    a mocked query_one/notify."""
    log_path = seeded_home / "agent-logs" / "T-3" / "reconcile-T-3-1000.000000.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line one\n", encoding="utf-8")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.write_json(claims.claim_path(seeded_home, "T-3"),
                         {"pid": proc.pid, "name": "reconcile-T-3",
                          "ts": store.iso_now(), "epoch": store.now_epoch() - 3600,
                          "log_path": str(log_path)})
        _run_modal_test(seeded_home, "T-3", "view_logs", LogsScreen)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_logs_screen_shows_third_format_log_not_blank(seeded_home):
    """AC4 (RF-3): a non-Claude ('opencode') session log renders its raw content
    in the real logs pane -- not a blank pane (the old failure mode for any
    filename the render path didn't recognize)."""
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-9999999998.000000.opencode.jsonl").write_text(
        '{"type": "message", "text": "hello from opencode"}\n', encoding="utf-8"
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert rendered.strip() != ""
            assert "hello from opencode" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_every_session_oldest_first_then_tails_live(seeded_home):
    """T-119: the Logs screen renders all captured sessions oldest-first under headers,
    then tails the live (claimed) one -- in the real mounted app."""
    import json as _json
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)

    def _stream(word):
        return _json.dumps({"type": "assistant", "message": {"id": word, "role": "assistant",
                            "content": [{"type": "text", "text": word}]}}) + "\n"

    (log_dir / "reconcile-T-3-1000.000000.stream.jsonl").write_text(_stream("said-old"), encoding="utf-8")
    (log_dir / "reconcile-T-3-2000.000000.stream.jsonl").write_text(_stream("said-mid"), encoding="utf-8")
    live = log_dir / "reconcile-T-3-3000.000000.stream.jsonl"
    live.write_text(_stream("said-live"), encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert rendered.index("said-old") < rendered.index("said-mid") < rendered.index("said-live")
            for sid in ("reconcile-T-3-1000.000000", "reconcile-T-3-2000.000000", "reconcile-T-3-3000.000000"):
                assert f"=== session {sid} | " in rendered
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_header_names_model(seeded_home):
    """T-120: each session's header in the mounted Logs screen names its model."""
    import json as _json
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-1000.000000.stream.jsonl").write_text(
        _json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"}) + "\n", encoding="utf-8")
    (log_dir / "reconcile-T-3-2000.000000.log").write_text("plain\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in app.screen.query_one("#logs-view", RichLog).lines)
            assert "runner: claude | model: claude-sonnet-5 ===" in rendered
            assert "model: unknown ===" in rendered
            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_opencode_tool_use_and_text(seeded_home):
    """AC4 (OC-5/T-41): opencode's own verified vocabulary (step_start/tool_use/
    text/step_finish) renders as structured content in the real logs pane, not
    just a raw byte dump."""
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": "Reading the ticket spec."}},
        {"type": "tool_use", "part": {"tool": "bash", "callID": "call_1",
                                       "state": {"input": {"command": "pytest"}}}},
        {"type": "step_finish", "part": {"reason": "stop", "cost": 0}},
    ]
    import json as _json
    (log_dir / "reconcile-T-3-9999999997.000000.opencode.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert "Reading the ticket spec." in rendered
            assert "bash" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_a_real_pi_log_without_exception(seeded_home):
    """AC11 (T-58): a `.pi.jsonl` log -- the real captured fixture, not a
    hand-authored one -- renders under a real `run_test()` mount without
    raising, and its tool-call/text content shows up structured in the pane
    (not just a raw byte dump)."""
    from textual.widgets import RichLog

    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / "fixtures" / "sample.pi.jsonl"
    (log_dir / "reconcile-T-3-9999999996.000000.pi.jsonl").write_text(
        fixture.read_text(encoding="utf-8"), encoding="utf-8"
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert rendered.strip() != ""
            assert "bash" in rendered
            assert "hello-pi-fixture" in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())


def test_logs_screen_renders_rate_limited_result_not_green(seeded_home):
    """T-18: a session log whose terminal result is is_error/429 must render as an
    error/rate-limit line in the real mounted logs pane, never green success."""
    from textual.widgets import RichLog

    fixture = Path(__file__).parent / "fixtures" / "rate_limited.stream.jsonl"
    log_dir = seeded_home / "agent-logs" / "T-3"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "reconcile-T-3-9999999999.000000.stream.jsonl").write_bytes(
        fixture.read_bytes()
    )

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-3"
            await app.run_action("view_logs")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], LogsScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_widget = app.screen.query_one("#logs-view", RichLog)
            rendered = "\n".join(strip.text for strip in log_widget.lines)
            assert "429" in rendered
            assert "rate_limited" in rendered
            assert "success" not in rendered

            assert app._exception is None
            await pilot.press("escape")
            await pilot.pause()
        assert app._exception is None

    asyncio.run(_inner())
