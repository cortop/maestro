"""Web `/deps` outline: guides, depth dot, chip, note and the frame class, over synthetic homes."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import seed_ticket  # noqa: E402
from maestro import store  # noqa: E402
from maestro.web.app import create_app  # noqa: E402


def seed(home, key, phase="implementing", dep_on=()):
    seed_ticket(home, key, f"title {key}", phase=phase)
    if dep_on:
        text = store.spec_path(home, key).read_text()
        text = text.replace("\n## Intent", f"dependsOn: [{', '.join(dep_on)}]\n\n## Intent", 1)
        store.atomic_write(store.spec_path(home, key), text)


def rows(html):
    out = {}
    for m in re.finditer(r'<li(?: class="([^"]*)")? data-key="([^"]+)" data-level="(\d+)">(.*?)</li>',
                         html, re.S):
        out.setdefault(m.group(2), m)  # the first row of a key is its expansion
    return out


def get(home, url="/deps?view=outline"):
    with TestClient(create_app(home)) as c:
        return c.get(url).text


def test_rows_in_order_with_guides_dot_chip_and_title(home):
    seed(home, "T-1")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-1"])
    seed(home, "T-4", "ready", ["T-2"])
    html = get(home)
    assert list(rows(html)) == ["T-1", "T-2", "T-4", "T-3"]
    r = rows(html)
    assert 'dep-guides' not in r["T-1"].group(4)
    assert '<i class="dep-g t"></i></span>' in r["T-2"].group(4)
    assert '<i class="dep-g v"></i><i class="dep-g l"></i>' in r["T-4"].group(4)
    assert '<i class="dep-g l"></i>' in r["T-3"].group(4)
    assert '<i class="dep-dot"></i>T-1' in r["T-1"].group(4)
    assert '<i class="dep-dot d1"></i>T-2' in r["T-2"].group(4)
    assert '<i class="dep-dot d2"></i>T-4' in r["T-4"].group(4)
    assert 'href="/t/T-2"' in r["T-2"].group(4)
    assert "title T-2" in r["T-2"].group(4) and "Queued" in r["T-2"].group(4)
    assert 'class="chip chip-queued"' in r["T-2"].group(4)


def test_twice_reachable_ticket_is_expanded_once(home):
    seed(home, "T-1")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-1"])
    seed(home, "T-4", "ready", ["T-2", "T-3"])
    html = get(home)
    assert html.count('data-key="T-4"') == 2
    reps = [m for m in re.finditer(r'<li class="([^"]*)" data-key="T-4"(.*?)</li>', html, re.S)]
    assert len(reps) == 1 and "is-rep" in reps[0].group(1)
    assert "shown above" in reps[0].group(2)
    assert html.count("shown above") == 1


def test_cycle_missing_and_multi_dependency_notes(home):
    seed(home, "T-1", dep_on=["T-2"])
    seed(home, "T-2", dep_on=["T-1"])
    seed(home, "T-3", dep_on=["T-99"])
    seed(home, "T-5")
    seed(home, "T-6")
    seed(home, "T-7", "ready", ["T-5", "T-6"])
    r = rows(get(home))
    assert "is-bad" in (r["T-1"].group(1) or "") and "in a cycle" in r["T-1"].group(4)
    assert "is-bad" in (r["T-3"].group(1) or "") and "missing T-99" in r["T-3"].group(4)
    assert "waits on 2" in r["T-7"].group(4)
    assert "is-bad" not in (r["T-7"].group(1) or "")


def test_outline_in_page_for_graph_and_frame_class_only_for_outline(home):
    seed(home, "T-1")
    seed(home, "T-2", "ready", ["T-1"])
    graph = get(home, "/deps?view=graph")
    assert 'class="dep-tree"' in graph and "v-outline" not in graph
    outline = get(home)
    assert 'class="dep-tree"' in outline and "dep-frame v-outline" in outline
