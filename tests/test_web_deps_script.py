"""Web `/deps` script hooks: the served script, the data-* the server prints, and the toolbar."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import store  # noqa: E402
from maestro.view import deps as deps_view, deps_layout  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def seed(home, key, phase="implementing", dep_on=()):
    seed_ticket(home, key, f"title {key}", phase=phase)
    if dep_on:
        text = store.spec_path(home, key).read_text()
        text = text.replace("\n## Intent", f"dependsOn: [{', '.join(dep_on)}]\n\n## Intent", 1)
        store.atomic_write(store.spec_path(home, key), text)


def attrs(tag):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def tags(html, cls):
    return [attrs(m) for m in re.findall(rf'<a class="{cls}[ "][^>]*>', html)]


def test_script_is_served_defer_loaded_and_free_of_imports_and_key_listeners(home):
    seed(home, "T-1")
    with TestClient(create_app(home)) as c:
        js = c.get("/static/deps.js")
        assert js.status_code == 200
        assert '<script src="/static/deps.js" defer></script>' in c.get("/deps").text
    src = js.text
    assert not re.search(r"\bimport\b|\brequire\b", src)
    assert not re.search(r"(document|window)\s*\.\s*addEventListener\(\s*[\"']key", src)


def test_cards_ghosts_and_edges_carry_the_graph(home, cfg):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-2", "T-77"])
    seed(home, "T-4", "ready", ["T-3"])
    with TestClient(create_app(home)) as c:
        html = c.get("/deps").text
    cards = {a["data-id"]: a for a in tags(html, "dep-node")}
    assert set(cards) == {"T-1", "T-2", "T-3", "T-4"}
    assert cards["T-1"]["data-deps"] == ""
    assert cards["T-3"]["data-deps"].split() == ["T-2", "?T-77"]
    ghost = tags(html, "dep-ghost")[0]
    assert ghost["data-id"] == "?T-77" and ghost["data-deps"] == ""
    v = deps_view.build(home, cfg)
    lay = deps_layout.layout(v.graph)
    for k, a in cards.items():
        lane = next(i for i, ks in enumerate(lay.lane_cards) if k in ks)
        assert (int(a["data-lane"]), int(a["data-row"])) == (lane, lay.lane_cards[lane].index(k))
    edges = [attrs(m) for m in re.findall(r'<g class="dep-edge[^>]*>', html)]
    assert {(e["data-s"], e["data-t"]) for e in edges} == {(e.source, e.target) for e in lay.edges}


def test_toolbar_has_the_search_box_and_three_zoom_buttons(home):
    seed(home, "T-1")
    with TestClient(create_app(home)) as c:
        html = c.get("/deps").text
    assert 'type="search" placeholder="Find a ticket" data-dep-find' in html
    assert re.findall(r'data-dep-zoom="(\w+)"', html) == ["out", "reset", "in"]
    assert html.index("data-dep-find") < html.index("Finished") < html.index("Compact") < html.index("dep-zoom")
