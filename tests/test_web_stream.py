"""`/stream`: SSE named change events with monotonic ids and Last-Event-ID refresh."""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import event_log, inbox  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def _read(client, n, headers=None, poke=None):
    if poke:
        threading.Timer(0.4, poke).start()
    events = []
    with client.stream("GET", "/stream", headers=headers or {}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        cur = {}
        for line in resp.iter_lines():
            if line.startswith("event:"):
                cur["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                cur["id"] = int(line.split(":", 1)[1])
            elif not line and cur:
                events.append(cur)
                cur = {}
    assert len(events) == n
    return events


def test_changes_emit_named_events_with_monotonic_ids(home):
    seed_ticket(home, "T-1", "one", phase="implementing")
    app = create_app(home, poll_interval=0.05, stream_limit=3)
    with TestClient(app) as client:
        def poke():
            event_log.append(home, "T-1", "Note", {"text": "x"}, actor="t")
            time.sleep(0.3)
            inbox.append_command(home, "T-1", "msg", {"text": "hi"})
        evs = _read(client, 3, poke=poke)
    names = [e["event"] for e in evs]
    assert "ticket:T-1" in names and "board" in names and "fleet" in names
    ids = [e["id"] for e in evs]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_reconnect_with_last_event_id_full_refresh(home):
    seed_ticket(home, "T-1", "one", phase="ready")
    seed_ticket(home, "T-2", "two", phase="ready")
    app = create_app(home, poll_interval=60, stream_limit=4)
    with TestClient(app) as client:
        evs = _read(client, 4, headers={"Last-Event-ID": "5"})
    assert [e["event"] for e in evs] == ["ticket:T-1", "ticket:T-2", "board", "fleet"]
    assert [e["id"] for e in evs] == sorted(e["id"] for e in evs)


def test_fragments_subscribe_to_their_ticket_event(home):
    seed_ticket(home, "T-1", "one", phase="ready")
    with TestClient(create_app(home)) as client:
        assert 'hx-trigger="sse:ticket:T-1"' in client.get("/t/T-1").text
