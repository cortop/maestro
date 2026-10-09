"""Mounted-app tests for the #pulse strip (T-174)."""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from maestro import dispatcher as disp_mod, store
from tui_support import _make_app


# --- T-174: #pulse strip ---------------------------------------------------

def _pulse_cfg(home, body="", spend_total=None):
    (home / "config.toml").write_text("[maestro]\n" + body, encoding="utf-8")
    if spend_total is not None:
        store.write_json(home / "derived" / ".spend.json",
                         {"date": store.utc_date(store.now_epoch()), "total_usd": spend_total})


def _pulse_ledger(home, key, n, weight=1):
    now = store.now_epoch()
    store.write_json(disp_mod._spawn_ledger_path(home),
                     {key: {"recent": [[now - 10 - i, weight] for i in range(n)]}})


async def _pulse_tick(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    app._refresh_pulse()
    await app.workers.wait_for_complete()
    await pilot.pause()
    return str(app.query_one("#pulse", Static).content)


@pytest.fixture
def no_probe(monkeypatch):
    from maestro import health
    monkeypatch.setattr(health, "_default_provider_probe", lambda host: (True, "reachable"))


def test_pulse_strip_renders_seeded_state(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "daily_spend_ceiling_usd = 10.0\nrunaway_spawns_per_hour = 50\n", 2.5)
    _pulse_ledger(seeded_home, "T-3", 7)
    store.write_json(store.heartbeat_path(seeded_home),
                     {"active": 3, "due": 5, "throttled": 2, "spawned": 0})

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            text = await _pulse_tick(app, pilot)
            assert "spawns 7/50 /hr" in text
            assert "$2.50 / $10.00" in text
            assert "3·5·2" in text
            assert "rate-limited" not in text
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_spend_thresholds(seeded_home, no_probe):
    async def _text(body, total):
        _pulse_cfg(seeded_home, body, total)
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            text = await _pulse_tick(app, pilot)
            assert app._exception is None
            return text

    async def _inner():
        cap = "daily_spend_ceiling_usd = 10.0\n"
        low = await _text(cap, 1.0)
        assert "$1.00 / $10.00" in low and "[yellow]$1.00" not in low and "[red bold]$1.00" not in low
        warn = await _text(cap, 5.0)
        assert "[yellow]$5.00[/yellow] / $10.00" in warn
        over = await _text(cap, 10.0)
        assert "[red bold]$10.00[/red bold] / $10.00" in over
        assert "no cap" in await _text("", 1.0)
        assert "unavailable" in await _text('session_log_format = "text"\n', None)

    asyncio.run(_inner())


def test_pulse_strip_rate_limit_segment(seeded_home, no_probe):
    _pulse_cfg(seeded_home)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            assert "rate-limited" not in await _pulse_tick(app, pilot)
            until = store.now_epoch() + 1800
            store.write_json(seeded_home / "derived" / ".ratelimit.json", {"paused_until": until})
            text = await _pulse_tick(app, pilot)
            from datetime import datetime, timezone
            hhmm = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M")
            assert f"rate-limited → {hhmm}" in text
            store.write_json(seeded_home / "derived" / ".ratelimit.json",
                             {"paused_until": store.now_epoch() - 5})
            assert "rate-limited" not in await _pulse_tick(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_config_error_shows_short_message(seeded_home, no_probe):
    (seeded_home / "config.toml").write_text("[maestro]\nbogus_key = 1\n", encoding="utf-8")

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            assert "pulse: config error" in await _pulse_tick(app, pilot)
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_toast_once_per_rising_edge(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "runaway_spawns_per_hour = 4\n")

    async def _inner():
        app = _make_app(seeded_home)
        toasts = []
        app.notify = lambda msg, **kw: toasts.append(msg)
        async with app.run_test(size=(160, 40)) as pilot:
            await _pulse_tick(app, pilot)
            assert toasts == []
            _pulse_ledger(seeded_home, "T-3", 9)
            for _ in range(3):
                await _pulse_tick(app, pilot)
            assert len(toasts) == 1 and "T-3" in toasts[0] and "F" in toasts[0]
            _pulse_ledger(seeded_home, "T-3", 1)
            await _pulse_tick(app, pilot)
            assert len(toasts) == 1
            _pulse_ledger(seeded_home, "T-4", 9)
            await _pulse_tick(app, pilot)
            await _pulse_tick(app, pilot)
            assert len(toasts) == 2 and "T-4" in toasts[1]
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_is_read_only(seeded_home, no_probe):
    _pulse_cfg(seeded_home, "daily_spend_ceiling_usd = 10.0\nrunaway_spawns_per_hour = 2\n")
    _pulse_ledger(seeded_home, "T-3", 5)
    store.write_json(seeded_home / "derived" / ".ratelimit.json",
                     {"paused_until": store.now_epoch() + 600})

    def _snapshot():
        return {p.relative_to(seeded_home): p.read_bytes()
                for p in (seeded_home / "derived").rglob("*")
                if p.is_file() and p.name != ".provider_probe_cache.json"}

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(160, 40)) as pilot:
            await _pulse_tick(app, pilot)
            before = _snapshot()
            for _ in range(3):
                await _pulse_tick(app, pilot)
            assert _snapshot() == before
            names = {p.name for p in before}
            assert ".spend.json" not in names and ".alarm.json" not in names
            assert app._exception is None

    asyncio.run(_inner())


def test_pulse_strip_does_not_collapse_filter_bar(seeded_home, no_probe):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await _pulse_tick(app, pilot)
            bar = app.query_one("#filter-bar", Static)
            pulse = app.query_one("#pulse", Static)
            header = app.query_one("Header")
            assert bar.region.height == 1 and pulse.region.height == 1
            assert bar.region.y != pulse.region.y
            assert min(bar.region.y, pulse.region.y) >= header.region.y + header.region.height
            assert app._exception is None

    asyncio.run(_inner())
