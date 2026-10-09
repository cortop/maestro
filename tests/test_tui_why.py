"""Mounted-app tests for the Why/Next screen (WhyScreen, `w`, T-165)."""
from __future__ import annotations

import asyncio
import json

from textual.widgets import DataTable, Static

from conftest import seed_phase
from maestro import (
    claims,
    config as config_mod,
    event_log,
    fleet as fleet_mod,
    dispatcher as disp_mod,
    ops as ops_mod,
    snapshot as snap_mod,
    store,
)
from maestro.statemachine import Phase
from maestro.tui import WhyScreen, _AcEvidenceModal, _ConfirmModal
from tui_support import _make_app, _settle


# --------------------------------------------------------------------------- #
# T-165: Why/Next screen (WhyScreen, `w`)                                      #
# --------------------------------------------------------------------------- #

def _why(app, sel):
    return str(app.screen.query_one(sel, Static).render())


def _seed_with_dep(home, key, dep, dep_phase="implementing"):
    for k, phase, deps in ((dep, dep_phase, ""), (key, "ready", dep)):
        store.atomic_write(store.spec_path(home, k),
                           f"# {k}\npriority: 2\ndependsOn: [{deps}]\n\n"
                           "## Acceptance criteria\n- [ ] ok\n")
        event_log.append(home, k, "TicketCreated",
                         {"title": k, "spec_hash": disp_mod.spec_hash_on_disk(home, k)}, actor="d")
        event_log.append(home, k, "PhaseChanged", {"phase": phase}, actor="r")
        snap_mod.rebuild(home, k)


def test_why_screen_blocked_dep(home):
    _seed_with_dep(home, "T-1", "T-2")

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            assert isinstance(app.screen, WhyScreen)
            now = _why(app, "#why-now")
            assert "blocked-dep" in now and "T-2 (implementing)" in now
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WhyScreen) and app.screen._key == "T-2"
            assert len(app.screen_stack) == 3


def test_why_screen_backoff_flips_to_timer(home):
    seed_phase(home, "T-1", Phase.AWAITING_CI)
    t0 = store.now_epoch()
    ops_mod.requeue(config_mod.load(str(home)), "T-1", 600)
    clock = [t0]

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.push_screen(WhyScreen(home, "T-1", clock=lambda: clock[0]))
            await _settle(app, pilot)
            assert "NOT DUE: backoff" in _why(app, "#why-now")
            clock[0] = snap_mod.load(home, "T-1").next_requeue_at + 1
            await pilot.pause(1.3)
            assert "DUE: timer" in _why(app, "#why-now")
            assert "NOT DUE" not in _why(app, "#why-now")


def test_why_screen_throttle_line(home):
    (home / "config.toml").write_text("[maestro]\nmin_spawn_interval = 300\n", encoding="utf-8")
    seed_phase(home, "T-1", Phase.READY)
    now = store.now_epoch()
    store.write_json(disp_mod._spawn_ledger_path(home),
                     {"T-1": {"last": now - 60, "recent": [[now - 60, 1]], "phase": "ready"}})

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            line = _why(app, "#why-throttle")
            assert "next spawn allowed in" in line and "PAUSED" not in line
            fleet_mod.pause(home, reason="maintenance")
            await pilot.press("r")
            await _settle(app, pilot)
            line = _why(app, "#why-throttle")
            assert "next spawn allowed in" in line and "fleet PAUSED (maintenance)" in line
            assert app._exception is None

    asyncio.run(_inner())


def _seed_decisions(home, key, n):
    lines = [json.dumps({"ts": f"2026-10-08T00:{i:02d}:00+00:00",
                         "decisions": {key: {"outcome": "not_due", "reason": f"reason-{i}"},
                                       "T-other": {"outcome": "spawned", "reason": "x"}}})
             for i in range(n)]
    (home / "derived").mkdir(exist_ok=True)
    disp_mod.dispatch_ledger_path(home).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_why_screen_decisions_table(home):
    seed_phase(home, "T-1", Phase.READY)
    _seed_decisions(home, "T-1", 25)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            table = app.screen.query_one("#why-decisions", DataTable)
            assert table.row_count == 20
            reasons = [str(table.get_row_at(i)[2]) for i in range(20)]
            assert "reason-24" in reasons and "reason-5" in reasons and "reason-4" not in reasons
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, _AcEvidenceModal)
            body = " ".join(str(w.render()) for w in app.screen.query(Static))
            assert "reason-24" in body
            assert app._exception is None

    asyncio.run(_inner())


def test_why_screen_is_read_only(home):
    seed_phase(home, "T-1", Phase.READY)
    seed_phase(home, "T-2", Phase.READY)
    _seed_decisions(home, "T-1", 3)
    disp_mod._write_heartbeat(home, store.now_epoch(), 0, 0)
    claims.write_claim(home, "T-2", 999999, "dead")  # stale: pid is not alive

    def _state():
        claim_files = sorted((p.name, p.stat().st_mtime_ns)
                             for p in (home / "derived" / "claims").glob("*.json"))
        return ((home / "derived" / ".heartbeat.json").stat().st_mtime_ns,
                len(disp_mod.dispatch_ledger_path(home).read_text().splitlines()), claim_files)

    before = _state()

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-2"
            await pilot.press("w")
            await _settle(app, pilot)
            assert "STALE" in _why(app, "#why-claim")
            await pilot.press("r")
            await _settle(app, pilot)
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())
    after = _state()
    assert claims.claim_path(home, "T-2").exists()
    assert after == before


def test_why_screen_kick_is_confirmed_and_scoped(home):
    seed_phase(home, "T-1", Phase.READY)
    seed_phase(home, "T-2", Phase.READY)

    async def _inner():
        app = _make_app(home)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await pilot.press("w")
            await _settle(app, pilot)
            await pilot.press("k")
            await pilot.pause()
            assert isinstance(app.screen, _ConfirmModal)
            await pilot.press("enter")  # default No
            await _settle(app, pilot)
            assert isinstance(app.screen, WhyScreen)
            assert app.dry.spawned == []
            await pilot.press("k")
            await pilot.pause()
            await pilot.press("y")
            await _settle(app, pilot)
            assert [sp[0] for sp in app.dry.spawned] == ["T-1"]
            assert app._exception is None

    asyncio.run(_inner())
