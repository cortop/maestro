"""view.deps_layout: lanes, overlaps, cycles, ghosts, stacks, finished lane, compact and only."""
from __future__ import annotations

import re

from conftest import seed_phase
from maestro import depgraph, store
from maestro.statemachine import Phase
from maestro.view import deps_layout as dl


def graph(deps: dict, missing: dict | None = None, done: dict | None = None) -> depgraph.DepGraph:
    """Synthetic DepGraph: *deps* maps every open key to its open dependencies."""
    nodes = {k: depgraph.Node(k, f"title {k}", "ready", list(v)) for k, v in deps.items()}
    for k, m in (missing or {}).items():
        nodes[k].missing = list(m)
    for k, d in (done or {}).items():
        nodes[k].done_keys = list(d)
        nodes[k].done_deps = len(d)
    dependents = {k: [] for k in nodes}
    for k in sorted(nodes, key=depgraph._natural):
        for d in nodes[k].open_deps:
            dependents[d].append(k)
    cycles = depgraph.find_cycles({k: n.open_deps for k, n in nodes.items()})
    return depgraph.DepGraph(nodes, dependents, [], cycles, {})


def boxes(lay):
    return [(c.id, c.x, c.y, c.w, c.h) for c in lay.cards + lay.ghosts + lay.stacks]


def overlaps(a, b):
    return a[1] < b[1] + b[3] and b[1] < a[1] + a[3] and a[2] < b[2] + b[4] and b[2] < a[2] + a[4]


def lane_of(lay, key):
    return next(c.lane for c in lay.cards if c.key == key)


def longest(deps, k, seen=()):
    return max((longest(deps, d, seen + (k,)) + 1 for d in deps[k] if d not in seen), default=0)


BOARD = {
    "T-1": [], "T-2": ["T-1"], "T-3": ["T-1"], "T-4": ["T-2", "T-3"],   # diamond
    "T-5": [], "T-6": ["T-5"], "T-7": ["T-6"], "T-8": ["T-7"], "T-9": ["T-5", "T-8"],  # spans 4 lanes
    "T-20": [], **{f"T-{30 + i}": ["T-20"] for i in range(12)},          # fan of 12
    "T-90": ["T-91"], "T-91": [],                                          # second group
    "T-99": [],                                                            # unlinked
}


def test_lanes_follow_longest_chain_and_are_deterministic():
    g = graph(BOARD)
    lay = dl.layout(g)
    for c in lay.cards:
        assert c.lane == longest(BOARD, c.key)
    for e in lay.edges:
        if not e.back and e.source in BOARD and e.target in BOARD:
            assert lane_of(lay, e.source) < lane_of(lay, e.target)
    assert dl.layout(g) == lay
    assert lay.unlinked == ["T-99"]


def test_no_overlap_and_one_slot_per_crossed_lane():
    lay = dl.layout(graph(BOARD))
    bs = boxes(lay)
    for i, a in enumerate(bs):
        for b in bs[i + 1:]:
            assert not overlaps(a, b), (a, b)
    # T-5 (lane 0) -> T-9 (lane 4): three slots, written as straight "L" runs
    edge = next(e for e in lay.edges if (e.source, e.target) == ("T-5", "T-9"))
    runs = re.findall(r"L (\d+) (\d+)", edge.d)
    assert len(runs) == 3
    slots = [(int(x) - lay.geometry.nw, int(y) - lay.geometry.dh // 2, lay.geometry.nw, lay.geometry.dh)
             for x, y in runs]
    slots = [(f"slot{i}", x, y, w, h) for i, (x, y, w, h) in enumerate(slots)]
    for s in slots:
        for b in bs:
            assert not overlaps(s, b), (s, b)


def test_cycle_and_self_dependency():
    cyc = {"C-1": ["C-3"], "C-2": ["C-1"], "C-3": ["C-2"], "S-1": ["S-1"], "A-1": []}
    lay = dl.layout(graph(cyc))
    cyc_edges = [e for e in lay.edges if e.kind == "cycle"]
    assert len(cyc_edges) == 4
    backs = [e for e in lay.edges if e.back]
    assert len(backs) == 2
    assert {(e.source, e.target) for e in backs} == {("C-1", "C-2"), ("S-1", "S-1")}
    for e in backs:
        by = int(re.search(r" L \d+ (\d+) C", e.d).group(1))
        bottoms = [c.y + c.h for c in lay.cards if c.key[0] == e.target[0]]
        assert by > max(bottoms)
        assert lay.height > by
    assert {e.kind for e in lay.edges} == {"cycle"}


def test_missing_dependency_is_one_ghost():
    g = graph({"M-1": [], "M-2": ["M-1"], "M-3": []}, missing={"M-2": ["X-1"], "M-3": ["X-1"]})
    lay = dl.layout(g)
    ghosts = [c for c in lay.ghosts if c.kind == "missing"]
    assert [c.key for c in ghosts] == ["X-1"]
    left = min(lane_of(lay, k) for k in ("M-2", "M-3"))
    assert ghosts[0].lane == left - 1
    miss = [e for e in lay.edges if e.kind == "missing"]
    assert {(e.source, e.target) for e in miss} == {("?X-1", "M-2"), ("?X-1", "M-3")}


def test_fan_stack_fold_and_selected():
    g = graph({"P-1": [], **{f"P-{10 + i}": ["P-1"] for i in range(12)}})
    lay = dl.layout(g)
    assert len([c for c in lay.cards if c.key != "P-1"]) == 4
    (stack,) = lay.stacks
    assert stack.kind == "stack" and len(stack.keys) == 8
    assert lay.lanes[1].count == 12

    lay = dl.layout(g, expanded=["P-1"])
    assert len(lay.cards) == 13
    (fold,) = lay.stacks
    assert fold.kind == "fold" and any(e.kind == "fold" for e in lay.edges)

    lay = dl.layout(g, selected="P-21")
    assert lay.stacks[0].kind == "fold" and len(lay.cards) == 13
    assert dl.layout(g, selected="P-10").stacks[0].kind == "stack"


def test_finished_lane_and_unlinked():
    g = graph({"F-1": [], "F-2": ["F-1"], "F-3": []},
              done={"F-2": ["D-1"], "F-3": ["D-1", "D-2"]})
    off = dl.layout(g)
    assert not [c for c in off.ghosts] and not any(e.kind == "done" for e in off.edges)
    assert off.unlinked == ["F-3"]
    on = dl.layout(g, finished=True)
    assert on.unlinked == []
    assert sorted(c.key for c in on.ghosts) == ["D-1", "D-2"]
    assert on.lane0 == 1
    done = {(e.source, e.target) for e in on.edges if e.kind == "done"}
    assert done == {("done:D-1", "F-2"), ("done:D-1", "F-3"), ("done:D-2", "F-3")}
    assert all(c.x < min(k.x for k in on.cards) for c in on.ghosts)


def test_compact_and_only():
    g = graph(BOARD)
    lay = dl.layout(g, compact=True)
    assert lay.geometry == dl.COMPACT
    assert {(c.w, c.h) for c in lay.cards} == {(176, 34)}
    one = dl.layout(g, only="T-2")
    assert sorted(c.key for c in one.cards) == ["T-1", "T-2", "T-4"]
    assert one.unlinked == []
    assert [c.key for c in dl.layout(g, only="T-99").cards] == ["T-99"]


def test_empty_graph():
    lay = dl.layout(graph({}))
    assert (lay.width, lay.height, lay.cards, lay.lanes) == (0, 0, [], [])


def test_build_records_done_keys(home):
    seed_phase(home, "A-1", Phase.DONE)
    seed_phase(home, "A-2", Phase.READY)
    store.spec_path(home, "A-2").write_text(
        "# A-2\n\ndependsOn: [A-1]\n\n## Acceptance criteria\n- [ ] ok\n", encoding="utf-8")
    node = depgraph.build(home).nodes["A-2"]
    assert node.done_keys == ["A-1"] and node.done_deps == 1
