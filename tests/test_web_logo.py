"""Web logo: static SVG files, dark-on-gold header mark, favicon on every page, README."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro.web.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "maestro" / "web" / "static"
MASK = "124,56 276,56 332,112 332,226 288,306 200,352 112,306 68,226 68,112"
EYES = ("98,150 184,172 184,192 108,182", "302,150 216,172 216,192 292,182")


def test_full_mark_shapes():
    svg = (STATIC / "logo.svg").read_text()
    assert 'viewBox="0 0 400 400"' in svg
    assert f'points="{MASK}"' in svg and 'stroke-width="6"' in svg
    for eye in EYES:
        assert f'points="{eye}"' in svg
    assert "M200 266 C 160 286, 116 266, 106 208" in svg
    assert "M200 266 C 240 286, 284 266, 294 208" in svg


def test_small_mark_shapes():
    svg = (STATIC / "logo-mark.svg").read_text()
    assert 'viewBox="56 44 288 320"' in svg
    assert f'points="{MASK}"' in svg and 'stroke-width="12"' in svg
    for eye in EYES:
        assert f'points="{eye}"' in svg
    assert "M200 270 C 160 290, 116 268, 106 208" in svg
    assert "M200 270 C 240 290, 284 268, 294 208" in svg


@pytest.mark.parametrize("name", ["logo.svg", "logo-mark.svg"])
def test_svg_is_inert(name):
    svg = (STATIC / name).read_text()
    assert "<script" not in svg and "href" not in svg
    assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", svg)


@pytest.mark.parametrize("path", ["/", "/tickets", "/fleet"])
def test_every_page_has_favicon_and_no_external_host(home, path):
    with TestClient(create_app(home)) as c:
        resp = c.get(path)
        assert resp.status_code == 200
        html = resp.text
        assert '<link rel="icon" type="image/svg+xml" href="/static/logo-mark.svg">' in html
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html)
        assert c.get("/static/logo-mark.svg").status_code == 200
        assert c.get("/static/logo.svg").status_code == 200


def test_header_mark_is_dark_on_gold(home):
    with TestClient(create_app(home)) as c:
        html = c.get("/").text
    header = re.search(r'<a class="logo".*?</a>', html, re.S).group(0)
    assert header.index("<svg") < header.index("maestro</a>")
    assert 'fill="#07070A"' in header and "stroke" not in header
    assert header.count('fill="#EBCB55"') == 4


def test_readme_shows_logo_first():
    readme = (ROOT / "README.md").read_text()
    assert readme.index("maestro/web/static/logo.svg") < readme.index("# maestro")
    assert (ROOT / "maestro/web/static/logo.svg").exists()
