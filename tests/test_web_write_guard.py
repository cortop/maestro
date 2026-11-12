"""Web write guard: token, Origin check, `--read-only`, and the non-loopback refusal."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from maestro import cli  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402

BUTTON = '<button class="btn-danger">Discard</button>'


def _mount(app):
    """Mount a synthetic POST route and a page that renders an action button."""
    calls = []

    async def act(request: Request):
        calls.append(1)
        return {"ok": True}

    app.state.guard.post(app, "/_test/act", act)

    @app.get("/_test/page")
    async def page(request: Request):
        tpl = ('{% extends "base.html" %}{% block body %}{% if can_write %}' + BUTTON
               + "{% endif %}{% endblock %}")
        shell = app.state.shell
        return shell.templates.TemplateResponse(
            request, shell.templates.env.from_string(tpl),
            {"home": "h", "nav": "now", "nav_items": (), "shell_status": shell.status(),
             "can_write": app.state.guard.writes_enabled, "write_token": app.state.guard.token})
    return calls


def _client(home, **kw):
    app = create_app(home, **kw)
    calls = _mount(app)
    return TestClient(app), app, calls


def test_valid_token_and_origin_pass(home):
    c, app, calls = _client(home)
    with c:
        h = {TOKEN_HEADER: app.state.guard.token, "Origin": "http://testserver"}
        assert c.post("/_test/act", headers=h).status_code == 200
        assert c.post("/_test/act", headers={TOKEN_HEADER: app.state.guard.token}).status_code == 200
    assert len(calls) == 2


def test_missing_or_wrong_token_is_403(home):
    c, app, calls = _client(home)
    with c:
        assert c.post("/_test/act").status_code == 403
        assert c.post("/_test/act", headers={TOKEN_HEADER: "nope"}).status_code == 403
    assert calls == []


def test_foreign_origin_is_403(home):
    c, app, calls = _client(home)
    with c:
        h = {TOKEN_HEADER: app.state.guard.token, "Origin": "http://evil.example"}
        assert c.post("/_test/act", headers=h).status_code == 403
    assert calls == []


def test_token_is_per_process_and_rendered_in_page(home):
    c1, a1, _ = _client(home)
    c2, a2, _ = _client(home)
    assert a1.state.guard.token != a2.state.guard.token
    with c1:
        html = c1.get("/_test/page").text
    assert a1.state.guard.token in html and "hx-headers" in html and BUTTON in html


def test_read_only_mounts_no_write_route_and_hides_buttons(home):
    c, app, calls = _client(home, read_only=True)
    with c:
        h = {TOKEN_HEADER: app.state.guard.token}
        assert c.post("/_test/act", headers=h).status_code in (404, 405)
        html = c.get("/_test/page").text
    assert BUTTON not in html and app.state.guard.token not in html
    assert calls == []


def test_non_loopback_refuses_writes_unless_allowed(home):
    c, app, calls = _client(home, host="0.0.0.0")
    with c:
        h = {TOKEN_HEADER: app.state.guard.token}
        assert c.post("/_test/act", headers=h).status_code == 403
        assert BUTTON not in c.get("/_test/page").text
    assert calls == []
    c, app, calls = _client(home, host="0.0.0.0", allow_remote_writes=True)
    with c:
        assert c.post("/_test/act", headers={TOKEN_HEADER: app.state.guard.token}).status_code == 200


def test_cli_flags_reach_the_app_and_help_drops_read_only(home, monkeypatch, capsys):
    uvicorn = pytest.importorskip("uvicorn")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert cli.main(["--home", str(home), "web", "--read-only"]) == 0
    assert seen["app"].state.guard.read_only
    assert cli.main(["--home", str(home), "web", "--host", "0.0.0.0", "--allow-remote-writes"]) == 0
    assert seen["app"].state.guard.writes_enabled
    with pytest.raises(SystemExit):
        cli.main(["web", "--help"])
    top = cli.build_parser().format_help()
    line = next(ln for ln in top.splitlines() if " web " in ln)
    assert "read-only" not in line
