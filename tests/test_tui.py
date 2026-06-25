"""Tests for `maestro tui` entrypoint."""
from __future__ import annotations

import sys
from unittest import mock

from maestro.cli import main


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
