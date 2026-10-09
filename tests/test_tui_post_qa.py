"""Mounted-app tests for the post-QA trigger action (`Q`, T-117)."""
from __future__ import annotations

import asyncio

import pytest
from textual.worker import WorkerFailed

from maestro import dispatcher as disp_mod
from tui_support import _make_app


# --------------------------------------------------------------------------- #
# T-117: "Q" -> action_trigger_post_qa -> dispatcher.trigger_post_qa_skill    #
# `trigger_post_qa_skill` is mocked as the external boundary (a real success #
# ultimately spawns a session, `ops_mod.suggest_acs` above is the same       #
# shape) -- these tests exist to catch a regression where the action built   #
# `Config` directly instead of `config_mod.load()`, silently ignoring       #
# `config.toml` and making every manual fire report "not configured"        #
# regardless of what the board actually has set.                            #
# --------------------------------------------------------------------------- #

def test_trigger_post_qa_action_notifies_when_no_ticket_selected(seeded_home):
    """The `key is None` guard the binding sweep exercises for every key."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = None
            notifications_before = len(app._notifications)
            await app.run_action("trigger_post_qa")
            await pilot.pause()
            assert len(app._notifications) > notifications_before
            assert app._exception is None

    asyncio.run(_inner())


def test_trigger_post_qa_action_loads_real_config_toml(seeded_home, monkeypatch):
    """Regression test for the `Config(home=...)` bug: with `post_qa_skill`
    genuinely set in `config.toml`, the `cfg` the action hands to
    `trigger_post_qa_skill` must carry that value -- a bare `Config` dataclass
    would resolve it to `None` and the ticket's repo binding would never see
    it, no matter what's on disk."""
    (seeded_home / "config.toml").write_text(
        '[maestro]\npost_qa_skill = "/post-qa-polish"\npost_qa_skill_runner = "pi"\n',
        encoding="utf-8")
    captured = {}

    def _fake_trigger(cfg, sessions, key, **kw):
        captured["post_qa_skill"] = cfg.post_qa_skill
        captured["key"] = key
        return {"key": key, "skill": cfg.post_qa_skill, "runner": "pi", "pid": 1}
    monkeypatch.setattr(disp_mod, "trigger_post_qa_skill", _fake_trigger)

    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            await app.run_action("trigger_post_qa")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None

    asyncio.run(_inner())
    assert captured == {"post_qa_skill": "/post-qa-polish", "key": "T-1"}


def test_trigger_post_qa_action_real_not_configured_error_notify(seeded_home):
    """End-to-end (no mocking of `trigger_post_qa_skill`): with no
    `post_qa_skill` on the board, the real dispatcher call raises
    `MaestroError` and the app surfaces it via notify instead of crashing --
    same posture as the "suggest-acs" error path."""
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_key = "T-1"
            notifications_before = len(app._notifications)
            await app.run_action("trigger_post_qa")
            with pytest.raises(WorkerFailed):
                await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._exception is None
            assert len(app._notifications) > notifications_before
            assert "no post_qa_skill configured" in list(app._notifications)[-1].message

    asyncio.run(_inner())
