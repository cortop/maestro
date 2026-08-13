"""Full-stack e2e: drive the deterministic plumbing over a temp MAESTRO_HOME and
confirm the mounted TUI reflects the resulting board — without launching a real
``claude`` reconciler.

This stitches together the layers a single unit test never crosses: inbox -> a
real dispatcher sweep (mint + due-selection + spawn boundary) -> event log -> the
snapshot fold -> the projection (``ticket_rows``) -> the live Textual app. The
spawn boundary is the injectable ``DryRunSessions``, which RECORDS would-be spawns
instead of executing ``claude -p``, so the flow is hermetic and offline.
"""
from __future__ import annotations

import asyncio

import pytest

from maestro import dispatcher as disp, inbox
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import seed_ticket

pytest.importorskip("textual", reason="requires the [tui] extra (textual)")

from textual.widgets import DataTable  # noqa: E402

from maestro.tui import MaestroTUI, _FILTERS  # noqa: E402


def _filter_idx(name: str) -> int:
    return next(i for i, (n, _) in enumerate(_FILTERS) if n == name)


def test_dispatch_sweep_then_tui_reflects_board(home, cfg):
    """A sweep mints an inbox request and selects the due ready ticket (recording,
    not launching, the spawn), then the mounted TUI renders the whole board and its
    needs-you filter narrows to the tickets that actually want a human."""
    # A spread of phases already on the board...
    seed_ticket(home, "T-1", "ready to implement", phase="ready")
    seed_ticket(home, "T-2", "blocked on a human", phase="awaiting-human",
                questions={"q1": "Which approach?"})
    seed_ticket(home, "T-3", "work in progress", phase="implementing", pr=15)
    # ...and a brand-new request entering through the human inbox.
    inbox.append_new(home, "build the new thing", key="T-9")

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    # Deterministic plumbing: minted from the inbox, picked the due READY ticket,
    # and NEVER launched a real `claude` — DryRunSessions only records tuples.
    assert "T-9" in report.minted
    assert "T-1" in report.spawned
    # GA-10 appended allowed_tools as a 7th element, GA-17 env_overlay as an 8th,
    # RF-2 runner as a 9th, OC-4 runner_model as a 10th (see DryRunSessions.spawned's
    # docstring) -- named unpack, not a length check, so this doesn't become a trap
    # the next appended spawn input has to dodge.
    for s in sessions.spawned:
        assert isinstance(s, tuple)
        (key, prompt, cwd, model, effort, disallowed_tools, allowed_tools, env_overlay, runner,
         runner_model) = s
    assert {k for k, *_ in sessions.spawned} == set(report.spawned)

    # Now mount the REAL TUI over that same home and confirm it renders the board.
    async def _inner():
        app = MaestroTUI(home=str(home))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._filter_idx = _filter_idx("all")
            app._populate()
            await pilot.pause()
            table = app.query_one("#tickets", DataTable)
            # T-1, T-2, T-3 seeded + T-9 minted by the sweep = 4 rows.
            assert table.row_count == 4
            assert app._exception is None

            # The needs-you filter narrows to the awaiting-human ticket.
            app._filter_idx = _filter_idx("needs-you")
            app._populate()
            await pilot.pause()
            assert app.query_one("#tickets", DataTable).row_count == 1
            assert app._exception is None

    asyncio.run(_inner())


def test_minted_ticket_starts_in_triaging(home, cfg):
    """The ticket the sweep mints from the inbox lands in TRIAGING with a spec on disk
    — the precondition the TUI relies on to show a freshly-created row."""
    from maestro import snapshot as snap_mod, store

    inbox.append_new(home, "a fresh idea", key="T-42")
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert "T-42" in report.minted
    assert store.spec_path(home, "T-42").exists()
    assert snap_mod.load(home, "T-42").phase == Phase.TRIAGING.value
