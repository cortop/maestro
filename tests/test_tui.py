"""Tests for `maestro tui` entrypoint."""
from __future__ import annotations

import sys
from unittest import mock

from maestro import event_log, snapshot as snap_mod
from maestro.cli import main
from maestro.projection import ticket_rows
from maestro.statemachine import Phase


def test_tui_missing_package_exits_2(tmp_path, capsys):
    """maestro tui exits 2 with an install hint when textual is not installed."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None  # None sentinel → ImportError on import

    try:
        rc = main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 2
    assert "pip install" in capsys.readouterr().err


def test_tui_install_hint_mentions_extra(tmp_path, capsys):
    """The install hint names the [tui] extra so users know what to install."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None

    try:
        main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert "[tui]" in capsys.readouterr().err


def test_tui_core_cli_unaffected_by_missing_textual(tmp_path, capsys):
    """Core CLI commands still work when textual is missing."""
    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = None

    try:
        # `maestro env` is a core command that must work regardless
        rc = main(["--home", str(tmp_path), "env"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 0


def test_tui_delegates_to_tui_main(tmp_path):
    """When textual is present, cmd_tui calls tui.main with the parsed args."""
    fake_main = mock.Mock(return_value=0)
    fake_tui_module = mock.MagicMock()
    fake_tui_module.main = fake_main

    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = fake_tui_module

    try:
        rc = main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert rc == 0
    fake_main.assert_called_once()


def test_tui_passes_home_to_main(tmp_path):
    """The --home argument is forwarded to tui.main as part of args."""
    received = []

    def capture_main(args):
        received.append(args)
        return 0

    fake_tui_module = mock.MagicMock()
    fake_tui_module.main = capture_main

    saved = sys.modules.pop("maestro.tui", None)
    sys.modules["maestro.tui"] = fake_tui_module

    try:
        main(["--home", str(tmp_path), "tui"])
    finally:
        del sys.modules["maestro.tui"]
        if saved is not None:
            sys.modules["maestro.tui"] = saved

    assert received
    assert received[0].home == str(tmp_path)


# --- ticket_rows (data layer, no textual required) ---------------------------

def test_ticket_rows_empty_home(home):
    """An empty home returns an empty list without crashing."""
    assert ticket_rows(home) == []


def test_ticket_rows_lists_ticket_columns(home):
    """ticket_rows returns one row per ticket with key/phase/title in the correct positions."""
    event_log.append(home, "T-1", "TicketCreated",
                     {"title": "My feature", "source": "test", "spec_hash": "abc"},
                     actor="test", step_id="tc-1")
    snap_mod.rebuild(home, "T-1")

    rows = ticket_rows(home)
    assert len(rows) == 1
    key, phase, title, pr, ci, tier, fails, row_key = rows[0]
    assert key == "T-1"
    assert phase == Phase.TRIAGING.value
    assert title == "My feature"
    assert pr == "—"
    assert ci == "—"
    assert row_key == "T-1"


def test_ticket_rows_pr_label(home):
    """A ticket with a PR shows '#<number>', one without shows '—'."""
    event_log.append(home, "T-1", "PrOpened",
                     {"number": 42, "url": "https://github.com/x/y/pull/42", "draft": True},
                     actor="r", step_id="pr-T-1")
    snap_mod.rebuild(home, "T-1")

    rows = ticket_rows(home)
    assert rows[0][3] == "#42"


def test_ticket_rows_phase_order(home):
    """Rows are sorted by WORKSTATE phase order (implementing before triaging)."""
    event_log.append(home, "A-1", "TicketCreated", {"title": "alpha"}, actor="d")
    event_log.append(home, "A-1", "PhaseChanged", {"phase": "triaging", "reason": ""},
                     actor="r")
    snap_mod.rebuild(home, "A-1")

    event_log.append(home, "B-1", "TicketCreated", {"title": "beta"}, actor="d")
    event_log.append(home, "B-1", "PhaseChanged", {"phase": "implementing", "reason": ""},
                     actor="r")
    snap_mod.rebuild(home, "B-1")

    rows = ticket_rows(home)
    keys = [r[0] for r in rows]
    assert keys.index("B-1") < keys.index("A-1"), "implementing should sort before triaging"
