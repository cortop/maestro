"""The dependency view-model over real seeded homes (synthetic tickets)."""
from __future__ import annotations

from conftest import seed_ticket
from maestro import store
from maestro.view import deps


def seed(home, key, phase="implementing", dep_on=()):
    seed_ticket(home, key, f"title {key}", phase=phase)
    text = store.spec_path(home, key).read_text()
    if dep_on:
        text = text.replace("\n## Intent", f"dependsOn: [{', '.join(dep_on)}]\n\n## Intent", 1)
        store.atomic_write(store.spec_path(home, key), text)


def chain(home):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-2"])


def test_waits_on_you_and_start_here(home, cfg):
    chain(home)
    v = deps.build(home, cfg)
    assert v.waits_on_you == ["T-2", "T-3"]
    assert v.headline.text == "2 wait on you." and v.headline.tone == "you"
    assert [(s.key, s.verb, s.behind) for s in v.start_here] == [("T-1", "Answer", 2)]
    assert deps.downstream(v.graph, "T-1") == ["T-2", "T-3"]
    assert deps.upstream(v.graph, "T-3") == ["T-2", "T-1"]
    assert v.tickets["T-1"].group == "you" and v.tickets["T-2"].group == "queued"


def test_singular_headline_and_degraded_verb(home, cfg):
    seed(home, "T-1", "degraded")
    seed(home, "T-2", "ready", ["T-1"])
    v = deps.build(home, cfg)
    assert v.headline.text == "1 waits on you."
    assert v.start_here[0].verb == "Retry"


def test_cycle_and_missing_never_start(home, cfg):
    seed(home, "T-1", "awaiting-human")
    seed(home, "T-2", "ready", ["T-3"])
    seed(home, "T-3", "ready", ["T-2"])
    seed(home, "T-4", "ready", ["T-3"])
    seed(home, "T-5", "ready", ["T-99"])
    seed(home, "T-6", "ready", ["T-1"])
    v = deps.build(home, cfg)
    assert v.never_starts == ["T-2", "T-3", "T-4", "T-5"]
    assert v.headline.text == "4 can never start." and v.headline.tone == "bad"
    assert v.start_here == []
    assert v.waits_on_you == ["T-6"]
    levels = {a.level: a for a in v.alerts}
    assert levels["fail"].text == "T-2 → T-3 → T-2" and levels["fail"].keys == ["T-2", "T-3"]
    assert levels["warn"].keys == ["T-5"] and "T-99" in levels["warn"].text
    assert "does not exist" in v.panels["T-5"].sentences[0]
    assert v.panels["T-2"].sentences[0].startswith("Part of a dependency cycle.")
    assert v.key_panels["T-99"].kind == "missing"


def test_counters_and_empty_board(home, cfg):
    v = deps.build(home, cfg)
    assert v.headline.text == "The board is clear." and v.headline.tone == "ok"
    assert (v.can_start, v.one_step, v.stuck_deeper, v.longest_chain) == (0, 0, 0, 0)
    chain(home)
    seed(home, "T-4", "ready")
    v = deps.build(home, cfg)
    d = v.graph.depth
    assert v.can_start == sum(x == 0 for x in d.values()) == 2
    assert (v.one_step, v.stuck_deeper, v.longest_chain) == (1, 1, 3)


def test_wait_their_turn_and_nothing_blocked(home, cfg):
    seed(home, "T-1")
    assert deps.build(home, cfg).headline.text == "Nothing is blocked."
    seed(home, "T-2", "ready", ["T-1"])
    h = deps.build(home, cfg).headline
    assert h.text == "1 waits its turn." and h.tone == ""


def test_unlinked_and_finished_links(home, cfg):
    seed(home, "T-1", "done")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3")
    seed(home, "T-4", "ready", ["T-3"])
    seed(home, "T-5")
    assert deps.build(home, cfg).unlinked == ["T-2", "T-5"]
    v = deps.build(home, cfg, finished_links=True)
    assert v.unlinked == ["T-5"]
    assert v.key_panels["T-1"].kind == "finished" and v.key_panels["T-1"].named_by == ["T-2"]


def test_panel(home, cfg):
    seed(home, "T-1", "done")
    seed(home, "T-2", "awaiting-human")
    seed(home, "T-3", "ready", ["T-1", "T-2"])
    seed(home, "T-4", "ready", ["T-3"])
    seed(home, "T-5", "ready", ["T-2", "T-3"])
    p = deps.build(home, cfg).panels["T-3"]
    assert [r.key for r in p.waits_on] == ["T-2"]
    assert [r.key for r in p.finished] == ["T-1"] and p.finished_count == 1
    assert [r.key for r in p.blocks] == ["T-4", "T-5"]
    assert p.sentences == ("Waits on T-2, which needs you.", "2 tickets wait behind it.")
    assert p.spec_set == "maestro spec-set T-3 --depends-on T-1,T-2"
    assert p.downstream == 2 and p.lane == 1
    v = deps.build(home, cfg)
    assert v.panels["T-5"].sentences[0] == "Waits on 2 tickets, 1 of which needs you."
    assert v.panels["T-4"].sentences == ("Waits on T-3 (ready).", "Nothing waits behind it.")
    assert v.panels["T-2"].sentences == ("Nothing is in its way. It is waiting for your answer.",
                                         "3 tickets wait behind it.")
    assert v.panels["T-2"].spec_set == "maestro spec-set T-2 --depends-on <KEY>"


def test_outline_expands_once_and_marks_repeat(home, cfg):
    seed(home, "T-1")
    seed(home, "T-2", "ready", ["T-1"])
    seed(home, "T-3", "ready", ["T-1"])
    seed(home, "T-4", "ready", ["T-2", "T-3"])
    seed(home, "T-5")
    rows = deps.build(home, cfg).outline
    assert [(r.key, r.level, r.repeat) for r in rows] == [
        ("T-1", 0, False), ("T-2", 1, False), ("T-4", 2, False),
        ("T-3", 1, False), ("T-4", 2, True), ("T-5", 0, False)]
    assert rows[2].trail == (True, True) and rows[4].trail == (True, False)
    assert rows[3].open_deps == 1 and rows[2].open_deps == 2
