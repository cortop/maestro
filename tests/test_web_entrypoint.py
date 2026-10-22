"""`maestro web`: bind defaults, the missing-extra exit, and the header home."""
from __future__ import annotations

import sys

import pytest

from maestro import cli


def test_defaults_and_flags():
    args = cli.build_parser().parse_args(["web"])
    assert (args.host, args.port) == ("127.0.0.1", 8765)
    args = cli.build_parser().parse_args(["web", "--host", "0.0.0.0", "--port", "9000"])
    assert (args.host, args.port) == ("0.0.0.0", 9000)


def test_missing_extra_exits_2_with_hint(home, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fastapi", None)
    for name in [m for m in sys.modules if m.startswith("maestro.web")]:
        monkeypatch.delitem(sys.modules, name)
    assert cli.main(["--home", str(home), "web"]) == 2
    assert "maestro-orchestrator[web]" in capsys.readouterr().err


def test_main_serves_loopback_by_default(home, monkeypatch):
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert cli.main(["--home", str(home), "web"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8765, "log_level": "warning"}]


def test_header_shows_resolved_home(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from maestro.web.app import create_app

    with TestClient(create_app(home)) as client:
        assert str(home.resolve()) in client.get("/").text
