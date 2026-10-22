"""`maestro.view` is stdlib-only and exposes the view-models the TUI and web UI share."""
from __future__ import annotations

import subprocess
import sys

from conftest import seed_ticket
from maestro import event_log, store
from maestro.config import Config
from maestro.view import board as board_view
from maestro.view import events as ev_view
from maestro.view import fleet as fleet_view
from maestro.view import ticket as ticket_view
from maestro.view import why as why_view


def test_importing_view_pulls_no_ui_framework():
    code = (
        "import sys, importlib, pkgutil, maestro.view as v\n"
        "for m in pkgutil.iter_modules(v.__path__): importlib.import_module('maestro.view.' + m.name)\n"
        "bad = [m for m in ('textual', 'rich', 'fastapi', 'jinja2') if m in sys.modules]\n"
        "print(bad); sys.exit(1 if bad else 0)\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_board_filters_and_counts(seeded_home):
    cfg = Config(home=seeded_home)
    needs = board_view.board(seeded_home, cfg, "needs-you")
    assert {r.key for r in needs.rows} == {"T-1", "T-2"}
    assert needs.needs_you == 2
    assert {r.key for r in board_view.board(seeded_home, cfg, "active").rows} == {"T-3", "T-5"}
    everything = board_view.board(seeded_home, cfg, "all")
    assert len(everything.rows) == 5 and everything.counts["all"] == 5


def test_unknown_filter_raises(seeded_home):
    import pytest

    with pytest.raises(ValueError):
        board_view.board(seeded_home, Config(home=seeded_home), "bogus")


def test_ticket_view_and_event_pages(home):
    seed_ticket(home, "T-9", "nine", phase="implementing")
    for i in range(5):
        event_log.append(home, "T-9", "Note", {"text": str(i)}, actor="t")
    tv = ticket_view.ticket(home, "T-9", store.now_epoch())
    assert tv.phase == "implementing" and tv.dwell[-1].current
    assert [a["index"] for a in tv.acs] == [1]
    seen: list[int] = []
    page = ticket_view.event_page(home, "T-9", limit=2)
    while True:
        seen += [e["seq"] for e in page.events]
        if page.next_before is None:
            break
        page = ticket_view.event_page(home, "T-9", page.next_before, limit=2)
    assert seen == sorted(set(seen), reverse=True) and len(seen) >= 3


def test_event_helpers_and_fleet_pulse(home):
    assert ev_view.category_of("PhaseChanged") == 1 and ev_view.category_of("Nope") == 6
    assert ev_view.fmt_duration(3700) == "1h01m"
    assert ev_view.event_summary({"payload": {"a": 1}}) == "a=1"
    seed_ticket(home, "T-9", "nine", phase="ready")
    p = fleet_view.pulse(home, Config(home=home))
    assert {"buckets", "spawns", "spend", "heartbeat"} <= set(p)
    assert fleet_view.sparkline([0, 0]) == "▁▁"
    assert why_view.fmt_wait(125) == "2m05s"
