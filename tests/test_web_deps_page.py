"""Web `/deps` page: the drawn dependency graph over real seeded homes (synthetic tickets)."""
from __future__ import annotations

import re
from pathlib import Path

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


def client(home):
    return TestClient(create_app(home))


def chain(home):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-2"])


def test_cards_and_edges_follow_the_layout(home, cfg):
    chain(home)
    seed(home, "T-9", "implementing")
    with client(home) as c:
        html = c.get("/deps").text
    lay = deps_layout.layout(deps_view.build(home, cfg).graph)
    cards = re.findall(r'<a class="dep-node ([^"]*)" href="[^"]*" hx-get="[^"]*" hx-target="#deps-page" '
                       r'hx-select="#deps-page" hx-swap="outerHTML" hx-push-url="true" '
                       r'data-key="([^"]+)" '
                       r'style="left: ([\d.]+)px;[^"]*"', html)
    assert [k for _, k, _ in cards] == [c.key for c in lay.cards] == ["T-1", "T-2", "T-3"]
    for (cls, key, left), card in zip(cards, lay.cards):
        assert float(left) == card.x
    assert "g-you" in cards[0][0] and "g-queued" in cards[1][0]
    assert re.search(r'data-key="T-1".*?<span class="chip chip-you">Needs you</span>', html, re.S)
    assert re.search(r'data-key="T-2".*?<span class="chip chip-queued">Queued</span>', html, re.S)
    assert '<span class="dep-out">2</span>' in html  # T-1 has two tickets behind it
    paths = re.findall(r'<g class="dep-edge k-(\w+)"><path class="dep-edge-line" d="([^"]+)">'
                       r'</path><path class="dep-edge-head" d="([^"]+)">', html)
    assert paths == [(e.kind, e.d, e.head) for e in lay.edges] and len(paths) == 2
    assert '<svg class="dep-edges"' in html
    assert '<div class="dep-lane" style="left: 2px' in html and "one step away" in html
    assert "behind a chain" in html
    assert re.search(r'dep-frame" style="height: 300px"', html)


def test_headline_counters_and_alerts_match_the_view(home, cfg):
    chain(home)
    with client(home) as c:
        html = c.get("/deps").text
    v = deps_view.build(home, cfg)
    assert v.headline.text == "2 wait on you."
    assert '2 wait on <span class="you">you.</span>' in html
    assert "Each ticket below is waiting for you and has others queued behind it." in html
    assert re.search(r'dep-start-verb">Answer</span><b>T-1</b><span class="dep-start-n">2 behind it', html)
    counters = re.findall(r"<dt>([^<]+)</dt><dd>(\d\d)</dd>", html)
    assert counters == [("Can start", f"{v.can_start:02d}"), ("One step away", f"{v.one_step:02d}"),
                        ("Stuck deeper", f"{v.stuck_deeper:02d}"),
                        ("Longest chain", f"{v.longest_chain:02d}")]
    assert "3 open · 3 linked · 2 dependencies" in html


def test_cycle_and_missing_alerts(home):
    seed(home, "T-1", dep_on=["T-2"])
    seed(home, "T-2", dep_on=["T-1"])
    seed(home, "T-3", dep_on=["T-77"])
    seed(home, "T-4", dep_on=["T-77"])
    with client(home) as c:
        html = c.get("/deps").text
    assert '<span class="dep-alert-tag">1 cycle</span>' in html
    assert "T-1 → T-2 → T-1" in html or "T-2 → T-1 → T-2" in html
    assert "Nothing in a cycle can start. Remove one dependsOn entry to break it." in html
    assert '<div class="dep-alert warn"' in html
    assert "T-3, T-4 depend on T-77, which does not exist" in html
    assert "Blocked until the spec names a real ticket: maestro spec-set T-3 --depends-on &lt;KEY&gt;" in html
    assert '<a class="dep-ghost k-missing' in html and "no such ticket" in html
    assert "is-cycle" in html and "dep-loop" in html
    assert 'class="dep-title">2 can never <span class="bad">start.</span>' in html or "can never" in html
    assert "A cycle or a dependency that does not exist." in html


def test_unlinked_tray_lists_and_caps(home):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1"])
    for i in range(10, 30):
        seed(home, f"T-{i}")
    with client(home) as c:
        html = c.get("/deps").text
        stage = html.split('class="dep-stage"')[1].split('class="dep-outline"')[0]
        tray = html.split('class="dep-tray"')[1]
        assert "Unlinked · 20" in html
        assert 'data-key="T-1"' in stage and 'data-key="T-10"' not in stage
        assert len(re.findall(r'class="dep-unlinked"', tray)) == 17
        assert 'href="/deps?tray=all">Show all 20</a>' in tray
        full = c.get("/deps?tray=all").text
        assert len(re.findall(r'class="dep-unlinked"', full)) == 20
        assert "Show all" not in full
        assert 'class="dep-tray"' not in c.get("/deps?view=outline").text


def test_finished_and_compact_params_are_kept_by_every_link(home):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1", "T-50"])
    seed(home, "T-50", "done")
    seed(home, "T-3")
    with client(home) as c:
        plain = c.get("/deps").text
        assert "dep-ghost k-done" not in plain and "is-compact" not in plain
        assert "Finished <span>1</span>" in plain
        fin = c.get("/deps?finished=1&compact=1").text
        assert 'class="dep-ghost k-done is-compact" href="/deps?finished=1&amp;compact=1&amp;select=done:T-50"' in fin
        assert ">finished</span><i>1</i>" in fin
        assert re.search(r'dep-node g-\w+ is-compact" href="[^"]*select=T-1"', fin)
        tab_out = re.search(r'class="dep-tab" href="([^"]+)"', fin).group(1)
        assert tab_out == "/deps?view=outline&amp;finished=1&amp;compact=1"
        assert 'class="dep-toggle on" href="/deps?compact=1" aria-pressed="true">Finished' in fin
        assert 'class="dep-toggle t-compact on" href="/deps?finished=1" aria-pressed="true">Compact' in fin
        assert 'hx-get="/deps/headline?finished=1&amp;compact=1"' in fin
        assert 'hx-get="/deps/main?finished=1&amp;compact=1"' in fin


def test_regions_refetch_on_board_events_and_fragments_match_the_page(home):
    chain(home)
    with client(home) as c:
        page = c.get("/deps?compact=1").text
        head = c.get("/deps/headline?compact=1").text
        main = c.get("/deps/main?compact=1").text
    for region, frag in (("deps-headline", head), ("deps-main", main)):
        m = re.search(rf'<(?:section|div) id="{region}"[^>]*hx-get="([^"]+)" hx-trigger="sse:board"[^>]*>', page)
        assert m and "compact=1" in m.group(1)
        assert frag.strip() in page
    assert 'id="deps-headline"' not in head and "dep-bar" not in main and "dep-bar" in page
    assert "dep-bar" not in head


def test_empty_states(home):
    with client(home) as c:
        html = c.get("/deps").text
        assert "No open tickets." in html
        assert "When tickets are filed, the ones that wait on each other show up here." in html
        assert "No open tickets, so nothing can be waiting." in html and "dep-keyline" not in html
    seed(home, "T-1")
    seed(home, "T-2")
    with client(home) as c:
        html = c.get("/deps").text
        assert "Nothing is waiting on another ticket." in html
        assert "Dependencies appear here as soon as a spec names one in dependsOn." in html
        assert "Unlinked · 2" in html and "dep-keyline" not in html
        assert "No open ticket depends on another." in html


def test_stylesheet_is_present_and_page_has_no_extra_inline_style(home):
    css = (Path(__file__).parent.parent / "maestro/web/static/maestro.css").read_text()
    assert "/* dependency view (/deps) */" in css and ".chip-you{background:var(--gold)" in css
    assert ".dep-tray-more{" in css and "a.dep-x{display:inline-flex" in css
    chain(home)
    with client(home) as c:
        html = c.get("/deps").text
        styles = re.findall(r'style="([^"]*)"', html)
        assert styles
        for s in styles:
            props = {p.split(":")[0].strip() for p in s.split(";") if p.strip()}
            assert props <= {"left", "top", "width", "height", "background-size",
                             "background-position"}, s
        assert c.get("/deps?view=outline").status_code == 200
        out = c.get("/deps?view=outline").text
        assert "dep-frame v-outline" in out and 'class="dep-tree"' in out and "dep-edges" not in out
