"""Web Fleet schedule panel: add/edit/enable/disable/remove over a temp home, guarded."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import fleet  # noqa: E402
from maestro.config import load  # noqa: E402
from maestro.web.app import create_app  # noqa: E402
from maestro.web.guard import TOKEN_HEADER  # noqa: E402

ACTIONS = ("add", "edit", "remove", "enable", "disable")


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    monkeypatch.setattr(fleet, "last_exit_code", lambda *a, **kw: None)


def _tasks(home):
    return load(str(home)).scheduled


def _post(home, path, data=None, token=True, **kw):
    app = create_app(home, **kw)
    with TestClient(app) as c:
        h = {TOKEN_HEADER: app.state.guard.token} if token else {}
        return c.post(path, data=data or {}, headers=h)


def test_full_lifecycle_and_panel_refresh(home):
    r = _post(home, "/fleet/schedule/add", {"name": "nightly", "prompt": "do it", "every": "6h"})
    assert r.status_code == 200 and 'id="schedule"' in r.text and "nightly" in r.text
    assert [t["name"] for t in _tasks(home)] == ["nightly"]
    assert "Disable" in r.text and "Remove task nightly" in r.text

    r = _post(home, "/fleet/schedule/edit",
              {"name": "nightly", "prompt": "new prompt", "every": "", "cron": "0 9 * * 1"})
    assert "cron 0 9 * * 1" in r.text
    t = _tasks(home)[0]
    assert t["prompt"] == "new prompt" and t["cron"] == "0 9 * * 1" and not t.get("every")

    r = _post(home, "/fleet/schedule/disable", {"name": "nightly"})
    assert "(disabled)" in r.text and "Enable" in r.text
    assert _tasks(home)[0]["enabled"] is False
    r = _post(home, "/fleet/schedule/enable", {"name": "nightly"})
    assert "(disabled)" not in r.text and _tasks(home)[0]["enabled"] is True

    r = _post(home, "/fleet/schedule/remove", {"name": "nightly"})
    assert "No scheduled tasks." in r.text and _tasks(home) == []


@pytest.mark.parametrize("data,msg", [
    ({"name": "x", "prompt": "", "every": "1h"}, "is required"),
    ({"name": "x", "prompt": "p"}, "exactly one of"),
    ({"name": "x", "prompt": "p", "every": "1h", "cron": "0 9 * * *"}, "exactly one of"),
])
def test_add_validation_errors_inline_and_change_nothing(home, data, msg):
    r = _post(home, "/fleet/schedule/add", data)
    assert r.status_code == 200 and 'role="alert"' in r.text and msg in r.text
    assert _tasks(home) == []


def test_edit_validation_error_keeps_task(home):
    _post(home, "/fleet/schedule/add", {"name": "a", "prompt": "p", "every": "1h"})
    r = _post(home, "/fleet/schedule/edit",
              {"name": "a", "prompt": "p", "every": "1h", "cron": "0 9 * * *"})
    assert "exactly one of" in r.text
    t = _tasks(home)[0]
    assert t["every"] == "1h" and not t.get("cron")
    assert "no task named" in _post(home, "/fleet/schedule/remove", {"name": "zzz"}).text


def test_read_only_hides_controls_and_refuses_posts(home):
    _post(home, "/fleet/schedule/add", {"name": "a", "prompt": "p", "every": "1h"})
    app = create_app(home, read_only=True)
    with TestClient(app) as c:
        page = c.get("/fleet").text
        assert 'id="schedule"' in page and "schedule-add" not in page
        assert "/fleet/schedule/" not in page
        for p in ACTIONS:
            assert c.post(f"/fleet/schedule/{p}", data={"name": "a"}).status_code in (404, 405)
    assert len(_tasks(home)) == 1


def test_routes_are_guarded(home):
    for p in ACTIONS:
        r = _post(home, f"/fleet/schedule/{p}", {"name": "a", "prompt": "p", "every": "1h"},
                  token=False)
        assert r.status_code == 403
    assert _tasks(home) == []
