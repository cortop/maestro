"""Web `/deps` pinning: select, only, open and mark over real seeded homes (synthetic tickets)."""
from __future__ import annotations

import html as htmllib
import re
from urllib.parse import parse_qs, urlsplit

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


def client(home):
    return TestClient(create_app(home))


def abc(home):
    """A blocks B blocks C, plus an unrelated pair X blocks Y."""
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-2"])
    seed(home, "T-8")
    seed(home, "T-9", "ready", ["T-8"])


def node(html, key):
    m = re.search(rf'<a class="dep-node ([^"]*)" href="([^"]*)"[^>]*data-key="{key}"', html)
    assert m, key
    return m.group(1).split(), htmllib.unescape(m.group(2))


def drawn(html, key):
    return re.search(rf'<a class="dep-node [^"]*"[^>]*data-key="{key}"', html) is not None


def edge_classes(html):
    return re.findall(r'<g class="dep-edge k-\w+ ?([^"]*)">', html)


def query(href):
    return parse_qs(urlsplit(href).query)


def test_pin_lights_the_chain_dims_the_rest_and_opens_the_panel(home):
    abc(home)
    with client(home) as c:
        html = c.get("/deps?select=T-2").text
    assert "is-sel" in node(html, "T-2")[0]
    assert "is-dim" not in node(html, "T-1")[0] and "is-dim" not in node(html, "T-3")[0]
    assert "is-dim" in node(html, "T-8")[0] and "is-dim" in node(html, "T-9")[0]
    cls = edge_classes(html)
    assert sorted(cls) == ["is-dim", "lit-down", "lit-up"]
    assert cls[0] == "is-dim"  # lit edges are printed last
    side = html[html.index('<aside class="dep-side panel"'):]
    assert '<h2 class="panel-title">T-2 ' in side
    assert 'class="dep-group g-up">Waits on · 1</h4>' in side
    assert 'class="dep-group">Blocks · 1</h4>' in side
    assert "Finished ·" not in side
    assert re.search(r'dep-li s-open" href="[^"]*select=T-1"', side)
    assert re.search(r'dep-li s-open" href="[^"]*select=T-3"', side)
    assert 'href="/t/T-2">Open T-2' in side
    assert "maestro spec-set T-2 --depends-on T-1" in side
    assert "Waits on T-1, which needs you." in side and "1 ticket waits behind it." in side
    assert "one step away" in side
    assert "<aside" not in c.get("/deps").text


def test_every_link_sets_select_keeps_params_and_the_pinned_one_clears_it(home):
    abc(home)
    seed(home, "T-20", "awaiting-human")
    seed(home, "T-21", "ready", ["T-20"])
    seed(home, "T-30")  # unlinked
    with client(home) as c:
        page = c.get("/deps?compact=1&select=T-2").text
        head = c.get("/deps/headline?compact=1").text
    assert query(node(page, "T-1")[1]) == {"compact": ["1"], "select": ["T-1"]}
    assert query(node(page, "T-2")[1]) == {"compact": ["1"]}
    unl = re.search(r'<a class="dep-unlinked[^"]*" href="([^"]*)"', page).group(1)
    assert query(htmllib.unescape(unl)) == {"compact": ["1"], "select": ["T-30"]}
    close = re.search(r'<a class="dep-x" href="([^"]*)"', page).group(1)
    assert query(htmllib.unescape(close)) == {"compact": ["1"]}
    chip = re.search(r'<a class="dep-start" href="([^"]*)"', head).group(1)
    assert query(htmllib.unescape(chip))["select"] in (["T-1"], ["T-20"])
    assert query(htmllib.unescape(chip))["compact"] == ["1"]
    assert 'hx-target="#deps-page"' in page and 'hx-push-url="true"' in page


def test_ghost_links_carry_select_ids(home):
    seed(home, "T-1", "ready", ["T-77", "T-50"])
    seed(home, "T-50", "done")
    with client(home) as c:
        html = c.get("/deps?finished=1").text
    hrefs = [htmllib.unescape(h) for h in re.findall(r'<a class="dep-ghost[^"]*" href="([^"]*)"', html)]
    assert sorted(query(h)["select"][0] for h in hrefs) == ["?T-77", "done:T-50"]


def test_only_draws_one_chain_with_a_pill_and_no_tray(home):
    abc(home)
    seed(home, "T-30")
    with client(home) as c:
        full = c.get("/deps?select=T-2").text
        html = c.get("/deps?only=T-2&select=T-2").text
    assert 'class="dep-tray"' in full and 'class="dep-tray"' not in html
    for k in ("T-1", "T-2", "T-3"):
        assert drawn(html, k)
    for k in ("T-8", "T-9", "T-30"):
        assert not drawn(html, k)
    pill = re.search(r'<a class="dep-pill" href="([^"]*)"[^>]*>Only T-2 and its chain', html)
    assert pill and query(htmllib.unescape(pill.group(1))) == {"select": ["T-2"]}
    assert html.index("dep-pill") < html.index("dep-toggle")
    side = html[html.index("<aside"):]
    assert "Show everything" in side and "Only this chain" not in side
    assert "Only this chain" in full
    only_link = re.search(r'href="([^"]*only=T-2[^"]*)"[^>]*>Only this chain', full)
    assert only_link


def stackboard(home):
    seed(home, "T-1", "awaiting-human")
    for i in range(2, 10):
        seed(home, f"T-{i}", "ready", ["T-1"])


def test_stack_card_opens_its_parent_and_the_fold_card_closes_it(home):
    stackboard(home)
    with client(home) as c:
        html = c.get("/deps").text
        m = re.search(r'<a class="dep-stack k-stack[^"]*" href="([^"]*)"[^>]*><b>\+4 more</b>', html)
        assert m and query(htmllib.unescape(m.group(1))) == {"open": ["T-1"]}
        assert not drawn(html, 'T-9')
        opened = c.get(htmllib.unescape(m.group(1))).text
    for i in range(2, 10):
        assert drawn(opened, f'T-{i}')
    f = re.search(r'<a class="dep-stack k-fold[^"]*" href="([^"]*)"[^>]*><b>Fold</b>'
                  r'<span class="dep-stack-sub">8 wait only on T-1</span>', opened)
    assert f and htmllib.unescape(f.group(1)) == "/deps"


def test_pinned_missing_and_finished_ghosts_light_the_tickets_naming_them(home):
    seed(home, "T-1", "ready", ["T-77"])
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-9")
    seed(home, "T-10", "ready", ["T-9"])
    with client(home) as c:
        miss = c.get("/deps?select=?T-77").text
    assert "is-sel" in re.search(r'<a class="dep-ghost k-missing ([^"]*)"', miss).group(1)
    assert "is-dim" not in node(miss, "T-1")[0] and "is-dim" not in node(miss, "T-2")[0]
    assert "is-dim" in node(miss, "T-9")[0]
    assert "lit-down" in edge_classes(miss)
    side = miss[miss.index("<aside"):]
    assert "No ticket has this key." in side and "Named by · 1" in side
    assert "dep-facts" not in side and "dep-side-actions" not in side
    seed(home, "T-50", "done")
    seed(home, "T-3", "ready", ["T-50"])
    with client(home) as c:
        fin = c.get("/deps?finished=1&select=done:T-50").text
    assert "is-sel" in re.search(r'<a class="dep-ghost k-done ([^"]*)"', fin).group(1)
    assert "is-dim" not in node(fin, "T-3")[0] and "is-dim" in node(fin, "T-9")[0]
    side = fin[fin.index("<aside"):]
    assert "Was needed by · 1" in side and "title T-50" in side
    assert "dep-side-actions" not in side


def test_show_in_graph_marks_the_concerned_tickets(home):
    seed(home, "T-1", "ready", ["T-2"])
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-77"])
    seed(home, "T-5")
    seed(home, "T-6", "ready", ["T-5"])
    with client(home) as c:
        page = c.get("/deps").text
        links = [htmllib.unescape(h) for h in
                 re.findall(r'class="btn btn-sm btn-ghost" href="([^"]*)"[^>]*>Show in graph', page)]
        assert len(links) == 2
        cyc = c.get(next(h for h in links if "mark=cycle" in h)).text
        miss = c.get(next(h for h in links if "missing" in h)).text
        both = c.get("/deps?mark=cycle&select=T-5").text
    assert "is-match" in node(cyc, "T-1")[0] and "is-match" in node(cyc, "T-2")[0]
    assert all("is-dim" in node(cyc, k)[0] for k in ("T-3", "T-5", "T-6"))
    assert "is-match" in node(miss, "T-3")[0] and "is-dim" in node(miss, "T-1")[0]
    assert "is-match" in re.search(r'<a class="dep-ghost k-missing ([^"]*)"', miss).group(1)
    assert "is-match" not in both and "is-sel" in node(both, "T-5")[0]


def test_ticket_page_links_to_the_graph(home):
    seed(home, "T-1")
    with client(home) as c:
        html = c.get("/t/T-1").text
    assert '<a href="/deps?select=T-1">See in graph</a>' in html
