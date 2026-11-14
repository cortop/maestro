"""Web ticket page: dispatcher decision history and the confirmed per-ticket sweep."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import dispatcher, store  # noqa: E402
from maestro.sessions import DryRunSessions  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402


def _client(home, stack, **kw):
    app = create_app(home, nudge_on_action=False, **kw)
    c = stack.enter_context(TestClient(app))
    c.headers[TOKEN_HEADER] = app.state.guard.token
    return c


@pytest.fixture
def seeded(home):
    seed_ticket(home, "T-1", "working", phase="implementing")
    seed_ticket(home, "T-2", "other", phase="implementing")
    return home


def test_history_lists_decisions_with_hook_errors(seeded, monkeypatch):
    home = seeded
    dispatcher._append_dispatch_ledger(home, {
        "ts": "2026-01-02T03:04:05+00:00", "epoch": 1.0, "hook_errors": {"mint": "OSError: boom"},
        "decisions": {"T-1": {"outcome": "throttled", "reason": "spawn floor"},
                      "T-2": {"outcome": "spawned", "reason": "other-only"}}})
    from contextlib import ExitStack
    with ExitStack() as st:
        c = _client(home, st)
        side = c.get("/t/T-1/side").text
        page = c.get("/t/T-1").text
    assert "2026-01-02 03:04:05" in side and "throttled — spawn floor" in side
    assert "mint: OSError: boom" in side and "other-only" not in side
    assert 'hx-trigger="sse:ticket:T-1"' in page and 'id="decision-history"' in page


def test_confirmed_sweep_runs_off_thread_and_reports(seeded):
    home = seeded
    sessions = DryRunSessions()
    seen = {}

    def sweep(cfg, key):
        seen["thread"] = threading.current_thread()
        return dispatcher.dispatch(cfg, sessions, store.now_epoch(), key_filter=[key])

    from contextlib import ExitStack
    with ExitStack() as st:
        c = _client(home, st, sweep=sweep)
        assert "Run a sweep for this ticket" in c.get("/t/T-1/side").text
        refused = c.post("/t/T-1/sweep", data={})
        assert refused.status_code == 409 and "needs confirmation" in refused.text
        r = c.post("/t/T-1/sweep", data={"confirm": "1"})
        assert r.status_code == 200
        assert 'id="sweep-result"' in r.text
        assert "T-1: spawned" in r.text or "T-1: skipped" in r.text
        assert seen["thread"] is not threading.main_thread()
        assert [d for d in dispatcher.key_decisions(home, "T-1")]
        assert dispatcher.key_decisions(home, "T-2") == []  # restricted to the one key
        assert c.post("/t/T-9/sweep", data={"confirm": "1"}).status_code == 409


def test_read_only_hides_button_refuses_post_keeps_history(seeded):
    home = seeded
    dispatcher._append_dispatch_ledger(home, {
        "ts": "2026-01-02T03:04:05+00:00", "epoch": 1.0, "hook_errors": {},
        "decisions": {"T-1": {"outcome": "not_due", "reason": "backoff"}}})
    from contextlib import ExitStack
    with ExitStack() as st:
        c = _client(home, st, read_only=True, sweep=lambda cfg, key: pytest.fail("ran"))
        side = c.get("/t/T-1/side").text
        assert "Run a sweep" not in side and "not_due — backoff" in side
        assert c.post("/t/T-1/sweep", data={"confirm": "1"}).status_code in (403, 404, 405)


def test_palette_lists_sweep_entry(seeded):
    from contextlib import ExitStack
    with ExitStack() as st:
        c = _client(seeded, st)
        assert "Run a sweep for T-1" in c.get("/palette?key=T-1").text
