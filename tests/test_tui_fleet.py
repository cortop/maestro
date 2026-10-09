"""Mounted-app tests for FleetScreen, header badges and the app-level pause/up/down flows."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from textual.widgets import Input, Static

from conftest import seed_phase
from maestro import fleet as fleet_mod, store
from maestro.statemachine import Phase
from maestro.tui import FleetScreen, _ConfirmModal, _IntervalModal, _PauseModal
from tui_support import _make_app, _run_modal_test


def test_fleet_screen_open_and_escape(seeded_home):
    _run_modal_test(seeded_home, None, "fleet_panel", FleetScreen)


def test_fleet_screen_shows_paused_until_when_rate_limited(seeded_home):
    """A real .ratelimit.json pause is picked up by FleetScreen's fleet-refresh
    worker and rendered into #fleet-status as a 'paused until HH:MM' line."""
    import time as time_mod

    from maestro import store

    until_ts = time_mod.time() + 3600
    store.write_json(seeded_home / "derived" / ".ratelimit.json", {
        "paused_until": until_ts, "resets_at": until_ts - 60,
        "rate_limit_type": "five_hour", "source_key": "T-1",
        "source_log": "x", "ts": store.iso_now(),
    })

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = app.screen_stack[-1].query_one("#fleet-status", Static).content
            text = rendered if isinstance(rendered, str) else str(rendered)
            assert "paused until" in text
            until_str = time_mod.strftime("%H:%M", time_mod.localtime(until_ts))
            assert until_str in text
            assert app._exception is None

    asyncio.run(_inner())


def test_interval_modal_inside_fleet_screen(seeded_home):
    """FleetScreen 'u' (fleet_up) opens the _IntervalModal; escape dismisses it."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.press("u")  # fleet_up -> push _IntervalModal
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _IntervalModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_reports_spawn_rate_from_health_report(seeded_home):
    """FleetScreen._load_status must return health.report(...) verbatim (no
    hand-rolled doctor dict, no second copy of the 1800s staleness threshold):
    mounting the real app and opening FleetScreen renders the spawn-rate line."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Spawns/hr" in content
            # GA-14: the line is relabeled with the unit doctor now reports
            # (agent-equivalents, not a bare session count) -- assert the
            # label itself, not just the "Spawns/hr" prefix, so a regression
            # back to session-counting can't slip the assertion.
            assert "Spawns/hr (agent-equiv)" in content
            assert "Runaway" in content
            assert "Spawn floor" in content
            # GA-11: added beside the spawn-rate line, not folded into it --
            # GA-14 rebases onto a panel that already has a spend line.
            assert "Spend today" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_disabled_spawn_floor_distinguishably(seeded_home):
    """GA-8: the effective spawn floor renders beside Spawns/hr / Runaway, and a
    disabled (0) floor reads as an explicit disabled state, not a bare '0'."""
    (seeded_home / "config.toml").write_text("[maestro]\nmin_spawn_interval = 0\n")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Spawn floor" in content
            assert "disabled" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_spend_unavailable_for_text_format(seeded_home):
    """GA-11 trap: a session_log_format = "text" home must render spend as
    explicitly unavailable, never a silent $0.00 (a zero would be a ceiling
    that can never fire)."""
    (seeded_home / "config.toml").write_text('[maestro]\nsession_log_format = "text"\n')

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Spend today" in content
            assert "unavailable" in content
            assert "$0.00" not in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_no_cap_for_unset_ceiling(seeded_home):
    """RB-8: with a stream-json meter available but no daily_spend_ceiling_usd
    configured (seeded_home writes no config.toml, so the ceiling defaults to
    None), the fleet panel must read as an explicit "no cap" warning -- never a
    blank or an omitted value, matching the wording style of the `unavailable`
    branch above."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Spend today" in content
            assert "no cap" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_renders_runaway_board_differently(seeded_home):
    """A board that has actually exceeded its spawn budget must render visibly
    differently (RUNAWAY, red) from the healthy seeded_home above."""
    from maestro import dispatcher as disp
    from maestro.config import Config
    from test_dispatcher import _EphemeralSessions
    (seeded_home / "config.toml").write_text(
        "[maestro]\nrunaway_spawns_per_hour = 1\n")
    seed_phase(seeded_home, "R-1", Phase.IN_REVIEW)
    cfg = Config(home=seeded_home, max_concurrency=1, min_spawn_interval=0)
    sessions = _EphemeralSessions()
    t0 = store.now_epoch()
    for i in range(3):
        disp.dispatch(cfg, sessions, now=t0 + i)  # 3 spawns > budget of 1

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "RUNAWAY" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_header_badge_shows_provider_no_network_without_opening_fleet_screen(seeded_home, monkeypatch):
    """T-89 (AC1): a provider/network outage is visible on the header badge --
    the ALWAYS-visible main board, no `F`/FleetScreen (``show=False``) needed.
    Same seed shape as ``test_fleet_screen_shows_provider_no_network_distinguishably``
    below, but never pushes FleetScreen."""
    import json as json_mod
    from maestro import health
    from maestro import store as store_mod

    key = "T-3"  # seed_ticket'd into `implementing` by the seeded_home fixture
    for epoch in (100.0, 200.0, 300.0):
        session_id = f"reconcile-{key}-{epoch:.6f}"
        path = store_mod.session_stream_path(seeded_home, key, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json_mod.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n" +
            json_mod.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "offline"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)  # let the threaded badge worker land
            badge = app.query_one("#fleet-badge", Static)
            assert "NO NETWORK" in str(badge.content)
            assert app._exception is None
            assert not any(isinstance(s, FleetScreen) for s in app.screen_stack)

    asyncio.run(_inner())


def test_header_badge_shows_no_provider_warning_on_a_healthy_board(seeded_home, monkeypatch):
    """T-89 (AC2): the healthy counterpart to the test above -- no false
    positive on a board with no error streak and a reachable probe."""
    from maestro import health

    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)
            content = str(app.query_one("#fleet-badge", Static).content)
            assert "NO NETWORK" not in content
            assert "ERRORING" not in content
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_provider_no_network_distinguishably(seeded_home, monkeypatch):
    """MTO-8: a fleet whose recent sessions all ended in a non-429 provider
    error, with the confirmation probe unable to reach the network, must
    render distinguishably (NO NETWORK, red) from the healthy seeded_home
    above -- proven by mounting the real app, matching the ``runaway`` test
    right above."""
    import json as json_mod
    from maestro import health, store

    key = "T-3"  # seed_ticket'd into `implementing` by the seeded_home fixture
    for epoch in (100.0, 200.0, 300.0):
        session_id = f"reconcile-{key}-{epoch:.6f}"
        path = store.session_stream_path(seeded_home, key, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json_mod.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n" +
            json_mod.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (False, "offline"))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            status = app.screen_stack[-1].query_one("#fleet-status", Static)
            content = str(status.content)
            assert "Provider" in content
            assert "NO NETWORK" in content
            assert app._exception is None

    asyncio.run(_inner())


def test_header_badge_shows_paused_state(seeded_home):
    """AC (T-15): the header badge reflects a paused board."""
    from maestro import fleet

    fleet.pause(seeded_home, reason="tui check")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause(0.1)  # let the threaded badge worker land
            badge = app.query_one("#fleet-badge", Static)
            assert "PAUSED" in str(badge.content)
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_screen_shows_paused_and_toggle_resumes(seeded_home):
    """AC (T-15): FleetScreen surfaces the paused state and the new 'P' binding
    (not 'p' — already project_rebuild in both binding tables) toggles it."""
    from maestro import fleet

    fleet.pause(seeded_home, reason="tui toggle")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.pause(0.1)  # let the status-load worker land
            status_widget = app.screen.query_one("#fleet-status", Static)
            assert "Paused" in str(status_widget.content)

            await pilot.press("P")  # toggle_pause -> resume confirm (was paused)
            await pilot.pause(0.1)
            await pilot.press("y")
            for _ in range(50):  # threaded worker: poll, don't race a fixed pause
                await pilot.pause(0.1)
                if fleet.pause_state(seeded_home, store.now_epoch()) is None:
                    break
            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            assert app._exception is None

            await pilot.press("P")  # toggle_pause -> _PauseModal (now unpaused)
            for _ in range(50):
                await pilot.pause(0.1)
                if isinstance(app.screen_stack[-1], _PauseModal):
                    break
            assert isinstance(app.screen_stack[-1], _PauseModal)
            await pilot.pause(0.1)  # let the modal take focus before answering
            await pilot.press("enter")  # both fields empty -> pause at once
            for _ in range(50):
                await pilot.pause(0.1)
                if fleet.pause_state(seeded_home, store.now_epoch()) is not None:
                    break
            assert fleet.pause_state(seeded_home, store.now_epoch()) is not None
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_up_worker_loads_config_and_passes_it_through(seeded_home, monkeypatch):
    """`fleet up` from the TUI must reach `fleet.up` with a loaded Config.

    The call happens inside a `run_worker` thread lambda, where a raise is a
    silent no-op at runtime -- nothing surfaces, the fleet just never comes up.
    So this drives the real binding through the real worker and asserts the
    kwargs that arrived. `fleet_mod.up` itself is the one mocked boundary: it
    shells `install.sh` and `launchctl`, which a test must never do.
    """
    seen = {}

    def _fake_up(home, interval=300, **kw):
        seen["home"] = home
        seen["interval"] = interval
        seen["cfg"] = kw.get("cfg")
        return {"action": "up", "interval": interval, "rc": 0, "stdout": "", "label": "x"}

    monkeypatch.setattr(fleet_mod, "up", _fake_up)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], FleetScreen)
            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _IntervalModal)
            await pilot.press("2", "0", "0", "enter")   # submit a real interval
            await pilot.pause()
            for _ in range(20):
                if "cfg" in seen:
                    break
                await pilot.pause(0.05)
            assert app._exception is None

    asyncio.run(_inner())

    assert seen.get("interval") == 200
    cfg = seen.get("cfg")
    assert cfg is not None, "fleet.up was called without a Config"
    from maestro.config import Config
    assert isinstance(cfg, Config)
    # The whole point: the runner dirs it needs come off this Config.
    from maestro import fleet as _f
    assert isinstance(_f.config_runner_dirs(cfg), list)


def test_fleet_down_and_pause_confirm(seeded_home, monkeypatch):
    from maestro import fleet
    calls = []
    monkeypatch.setattr("maestro.fleet.down", lambda *a, **kw: calls.append(a))

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.run_action("fleet_panel")
            await pilot.pause(0.2)
            assert isinstance(app.screen_stack[-1], FleetScreen)

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == []

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(calls) == 1

            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            await pilot.press("P")  # _PauseModal replaced the plain pause confirm
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], _PauseModal)
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert fleet.pause_state(seeded_home, store.now_epoch()) is None
            assert app._exception is None

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-168: app-level P pause/resume + FleetScreen hardening                      #
# --------------------------------------------------------------------------- #

async def _await_cond(pilot, pred, n=50):
    for _ in range(n):
        await pilot.pause(0.1)
        if pred():
            return
    assert pred()


async def _open_fleet(app, pilot):
    await pilot.pause()
    await app.run_action("fleet_panel")
    await pilot.pause()
    assert isinstance(app.screen, FleetScreen)
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _type(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)


def test_app_pause_key_pauses_with_duration_and_reason(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = time.time()
            await pilot.press("P")
            await _await_cond(pilot, lambda: isinstance(app.screen, _PauseModal))
            await _type(pilot, "1h")
            await pilot.press("enter")
            await _type(pilot, "runaway")
            await pilot.press("enter")
            await _await_cond(pilot, lambda: fleet_mod.pause_state(seeded_home, store.now_epoch()))
            st = fleet_mod.pause_state(seeded_home, store.now_epoch())
            assert abs(st["until"] - (before + 3600)) < 5
            assert st["reason"] == "runaway"
            await app.workers.wait_for_complete()
            await pilot.pause()
            badge = app.screen_stack[0].query_one("#fleet-badge", Static).content
            assert "PAUSED" in str(badge)
            assert app._exception is None

    asyncio.run(_inner())


def test_app_pause_key_escape_writes_nothing(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("P")
            await _await_cond(pilot, lambda: isinstance(app.screen, _PauseModal))
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not fleet_mod.pause_path(seeded_home).exists()
            assert app._exception is None

    asyncio.run(_inner())


def test_pause_modal_rejects_bad_duration(seeded_home):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("P")
            await _await_cond(pilot, lambda: isinstance(app.screen, _PauseModal))
            await _type(pilot, "soon")
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, _PauseModal)
            assert str(app.screen.query_one("#pause-error").content).strip()
            assert not fleet_mod.pause_path(seeded_home).exists()
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not fleet_mod.pause_path(seeded_home).exists()
            assert app._exception is None

    asyncio.run(_inner())


def test_pause_key_offers_resume_from_disk_state(seeded_home):
    fleet_mod.pause(seeded_home, reason="x")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            app.screen._status = {}  # state before the first status load
            await pilot.press("P")
            await _await_cond(pilot, lambda: isinstance(app.screen, _ConfirmModal))
            assert not isinstance(app.screen, _PauseModal)
            await pilot.pause(0.1)
            await pilot.press("enter")  # default-No: Cancel focused
            await _await_cond(pilot, lambda: isinstance(app.screen, FleetScreen))
            assert fleet_mod.pause_state(seeded_home, store.now_epoch()) is not None
            await pilot.press("P")
            await _await_cond(pilot, lambda: isinstance(app.screen, _ConfirmModal))
            await pilot.pause(0.1)
            await pilot.press("y")
            await _await_cond(pilot, lambda: fleet_mod.pause_state(seeded_home, store.now_epoch()) is None)
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_up_rejects_non_integer_interval(seeded_home, monkeypatch):
    calls = []
    monkeypatch.setattr(fleet_mod, "up", lambda home, **kw: calls.append(kw) or {"ok": True})

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            await pilot.press("u")
            await _await_cond(pilot, lambda: isinstance(app.screen, _IntervalModal))
            await _type(pilot, "abc")
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, _IntervalModal)
            assert "not an integer" in str(app.screen.query_one("#interval-error").content)
            assert calls == []
            app.screen.query_one("#interval-input", Input).value = ""
            await _type(pilot, "120")
            await pilot.press("enter")
            await _await_cond(pilot, lambda: len(calls) == 1)
            assert calls[0]["interval"] == 120
            assert app._exception is None

    asyncio.run(_inner())


def test_fleet_clear_rate_limit_key(seeded_home):
    from maestro import ratelimit

    def _has(app):
        return any(getattr(x.binding, "action", None) == "clear_rate_limit"
                   for x in app.screen.active_bindings.values())

    async def _plain():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            assert not _has(app)

    asyncio.run(_plain())

    until_ts = time.time() + 3600
    store.write_json(seeded_home / "derived" / ".ratelimit.json", {
        "paused_until": until_ts, "resets_at": until_ts - 60,
        "rate_limit_type": "five_hour", "source_key": "T-1",
        "source_log": "x", "ts": store.iso_now(),
    })

    async def _limited():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            assert _has(app)
            await pilot.press("R")
            await _await_cond(pilot, lambda: isinstance(app.screen, _ConfirmModal))
            await pilot.pause(0.1)
            await pilot.press("y")
            await _await_cond(pilot, lambda: not ratelimit.status(seeded_home, time.time())["paused"])
            assert app._exception is None

    asyncio.run(_limited())


def test_fleet_mutations_log_result_and_error(seeded_home, monkeypatch):
    async def _log_text(app):
        return "\n".join(line.text for line in app.screen.query_one("#fleet-log").lines)

    async def _inner(fn, needle):
        monkeypatch.setattr(fleet_mod, "down", fn)
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            await pilot.press("d")
            await _await_cond(pilot, lambda: isinstance(app.screen, _ConfirmModal))
            await pilot.pause(0.1)
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert needle in await _log_text(app)
            assert app._exception is None

    asyncio.run(_inner(lambda home: {"stopped": "marker-ok"}, "marker-ok"))

    def _boom(home):
        raise RuntimeError("marker-boom")

    asyncio.run(_inner(_boom, "marker-boom"))


def test_fleet_real_sweep_resolves_executable_without_timeout(seeded_home, monkeypatch):
    import shutil as shutil_mod

    calls = []

    def _rec(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    async def _inner(which, first):
        calls.clear()
        monkeypatch.setattr(shutil_mod, "which", lambda name: which)
        monkeypatch.setattr(subprocess, "run", _rec)
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _open_fleet(app, pilot)
            await pilot.press("S")
            await _await_cond(pilot, lambda: isinstance(app.screen, _ConfirmModal))
            await pilot.pause(0.1)
            await pilot.press("y")
            await _await_cond(pilot, lambda: len(calls) >= 1)
            await app.workers.wait_for_complete()
            sweeps = [c for c in calls if "dispatch" in c[0]]
            assert len(sweeps) == 1
            argv, kw = sweeps[0]
            assert argv[:len(first)] == first
            assert argv[len(first):len(first) + 3] == ["--home", str(seeded_home), "dispatch"]
            assert "timeout" not in kw

    asyncio.run(_inner(None, [sys.executable, "-m", "maestro.cli"]))
    asyncio.run(_inner("/x/maestro", ["/x/maestro"]))
