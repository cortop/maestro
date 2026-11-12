"""Neon Ledger shell: header nav, fleet state, spend, no external hosts, reduced motion."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from maestro import fleet, store  # noqa: E402
from maestro.web import app as web_app  # noqa: E402
from maestro.web.app import create_app  # noqa: E402

WEB = Path(web_app.__file__).parent
CSS = (WEB / "static" / "maestro.css").read_text()


@pytest.fixture
def client(seeded_home):
    with TestClient(create_app(seeded_home)) as c:
        yield c


def test_css_defines_palette_and_components():
    for tok in ("#07070A", "#0E0E14", "#16161E", "#23232E", "#2E2E3A", "#3A3A48", "#F2F2F0",
                "#C9C9D4", "#9A9AA8", "#EBCB55", "#8F7C33", "#2B2612", "#00E5FF", "#FF2A6D",
                "#FF7FA6", "#9E2A50"):
        assert re.search(r"--[\w-]+:\s*" + tok, CSS, re.I), tok
    for cls in (".site-header", ".card", ".panel", ".chip", ".btn-primary", ".btn-secondary",
                ".btn-danger", ".ledger", ".rail"):
        assert cls in CSS, cls
    assert "clip-path" in CSS


def test_header_links_state_and_spend(client):
    html = client.get("/").text
    for href, label in (("/", "Now"), ("/tickets", "Tickets"), ("/fleet", "Fleet")):
        assert f'href="{href}"' in html and f">{label}</a>" in html
    assert re.search(r'class="current" aria-current="page">Now<', html)
    assert "fleet live" in html and "spend" in html


def test_header_shows_paused_state(client, seeded_home):
    fleet.pause(seeded_home, reason="test")
    assert "fleet paused" in client.get("/").text


def test_header_spend_against_ceiling(seeded_home):
    (seeded_home / "config.toml").write_text(
        "[maestro]\ndaily_spend_ceiling_usd = 50\nsession_log_format = \"stream-json\"\n")
    store.write_json(seeded_home / "derived" / ".spend.json",
                     {"date": store.utc_date(store.now_epoch()), "total_usd": 12.5})
    with TestClient(create_app(seeded_home)) as c:
        assert "$12.50 / $50.00" in c.get("/").text


def test_ticket_page_marks_tickets_current(client):
    html = client.get("/t/T-1").text
    assert re.search(r'class="current" aria-current="page">Tickets<', html)


def test_no_external_hosts_and_font_fallbacks():
    for f in [*(WEB / "templates").glob("*.html"), WEB / "static" / "maestro.css"]:
        assert not re.search(r"(https?:)?//[\w.-]+\.\w", f.read_text()), f.name
    assert not list((WEB / "static" / "fonts").glob("*.woff2")), "fixture assumes no bundled fonts"
    for var in ("--font-display", "--font-ui", "--font-mono"):
        stack = re.search(var + r":([^;]+);", CSS).group(1)
        assert re.search(r"system-ui|ui-monospace|sans-serif", stack)


def test_reduced_motion_disables_animation():
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", CSS, re.S)
    assert m and "animation: none" in m.group(1)
