"""Web event-stream lifecycle: every page loads the script that closes `/stream` when the page is left."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro.web import app as web_app  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

STATIC = Path(web_app.__file__).parent / "static"


def test_every_page_loads_the_stream_script_after_the_sse_extension(seeded_home):
    with TestClient(create_app(seeded_home)) as c:
        for path in ("/", "/tickets", "/fleet", "/t/T-1"):
            html = c.get(path).text
            assert 'sse-connect="/stream"' in html
            # it wraps the extension's EventSource factory, so it has to load after the extension
            assert html.index("/static/htmx-ext-sse.js") < html.index("/static/stream.js")
        assert c.get("/static/stream.js").status_code == 200


def test_stream_script_closes_on_pagehide_and_reloads_a_restored_page():
    js = (STATIC / "stream.js").read_text()
    assert "htmx.createEventSource" in js
    hide = js.split('"pagehide"')[1].split('"pageshow"')[0]
    assert ".close()" in hide
    show = js.split('"pageshow"')[1]
    assert "persisted" in show and "reload()" in show
