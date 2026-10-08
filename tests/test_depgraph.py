"""depgraph.build depth/roots/cycles and health.check_depends_on parity."""
from __future__ import annotations

from conftest import seed_phase
from maestro import config as config_mod, depgraph, health, store
from maestro.statemachine import Phase


def _ticket(home, key, deps=(), phase=Phase.READY):
    seed_phase(home, key, phase)
    if deps:
        p = store.spec_path(home, key)
        p.write_text(f"# {key}\n\ndependsOn: [{', '.join(deps)}]\n\n"
                     "## Acceptance criteria\n- [ ] ok\n", encoding="utf-8")


def test_chain_depths(home):
    _ticket(home, "A-1")
    _ticket(home, "A-2", ["A-1"])
    _ticket(home, "A-3", ["A-2"])
    g = depgraph.build(home)
    assert [g.depth[k] for k in ("A-1", "A-2", "A-3")] == [0, 1, 2]
    assert g.roots == ["A-1"]
    assert g.dependents["A-1"] == ["A-2"]


def test_diamond_uses_longest_path(home):
    _ticket(home, "A-1")
    _ticket(home, "A-2", ["A-1"])
    _ticket(home, "A-3", ["A-1", "A-2"])
    assert depgraph.build(home).depth["A-3"] == 2


def test_done_dep_ignored_but_counted(home):
    _ticket(home, "A-1", phase=Phase.DONE)
    _ticket(home, "A-2", ["A-1"])
    g = depgraph.build(home)
    assert "A-1" not in g.nodes
    assert g.depth["A-2"] == 0
    assert g.nodes["A-2"].done_deps == 1 and not g.nodes["A-2"].blocked
    assert g.roots == ["A-2"]


def test_missing_dep_is_depth_one(home):
    _ticket(home, "A-2", ["A-99"])
    g = depgraph.build(home)
    assert g.depth["A-2"] == 1
    assert g.nodes["A-2"].missing == ["A-99"] and g.nodes["A-2"].blocked


def test_cycle_members_are_roots_with_depth_ge_2(home):
    _ticket(home, "A-1", ["A-2"])
    _ticket(home, "A-2", ["A-1"])
    g = depgraph.build(home)
    assert g.cycles
    assert set(g.roots) == {"A-1", "A-2"}
    assert g.depth["A-1"] >= 2 and g.depth["A-2"] >= 2


def test_lone_ticket_is_root_and_roots_with_dependents_come_first(home):
    _ticket(home, "A-1")            # lone
    _ticket(home, "A-2")            # has a dependent
    _ticket(home, "A-3", ["A-2"])
    g = depgraph.build(home)
    assert g.roots == ["A-2", "A-1"]
    assert g.depth["A-1"] == 0


def test_check_depends_on_output_unchanged(home):
    _ticket(home, "A-1", ["A-2", "A-404"])
    _ticket(home, "A-2", ["A-1"])
    out = health.check_depends_on(config_mod.load(str(home)), 0.0)
    assert out["status"] == "fail"
    assert out["missing"] == [{"key": "A-1", "dep": "A-404"}]
    assert out["cycles"] == [["A-1", "A-2", "A-1"]]
    assert out["detail"] == "1 missing dep(s), 1 cycle(s)"
