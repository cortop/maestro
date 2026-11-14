"""Dependency view-model -- who waits on what, headline, alerts, panels and outline rows."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import depgraph, fleet as fleet_mod, snapshot as snap_mod, store
from ..config import Config
from ..statemachine import Phase
from . import board

START_HERE_MAX = 4
_DISP_PHASES = frozenset({"verifying", "awaiting-ci", "in-review", "terminating"})


def group_of(phase: str) -> str:
    """Colour group of an open ticket's phase."""
    if phase == Phase.AWAITING_HUMAN.value:
        return "you"
    if phase == Phase.DEGRADED.value:
        return "bad"
    if phase in _DISP_PHASES:
        return "disp"
    if phase == Phase.READY.value:
        return "queued"
    return "agent"


def _reach(start: str, edges: dict[str, list[str]]) -> list[str]:
    seen: dict[str, None] = {}
    stack = list(reversed(edges.get(start, [])))
    while stack:
        k = stack.pop()
        if k in seen or k == start:
            continue
        seen[k] = None
        stack.extend(reversed(edges.get(k, [])))
    return list(seen)


def upstream(g: depgraph.DepGraph, key: str) -> list[str]:
    """Every open ticket *key* transitively waits on, itself excluded."""
    return _reach(key, {k: n.open_deps for k, n in g.nodes.items()})


def downstream(g: depgraph.DepGraph, key: str) -> list[str]:
    """Every open ticket transitively waiting behind *key*, itself excluded."""
    return _reach(key, g.dependents)


@dataclass
class TicketInfo:
    key: str
    title: str
    phase: str
    group: str
    held: bool
    live: bool
    depth: int
    in_cycle: bool
    downstream: int


@dataclass
class Headline:
    lead: str
    last: str
    tone: str  # ok | bad | you | ""

    @property
    def text(self) -> str:
        return f"{self.lead} {self.last}"


@dataclass
class StartHere:
    key: str
    verb: str
    behind: int


@dataclass
class Alert:
    level: str  # fail | warn
    text: str
    keys: list[str]


@dataclass
class DepRow:
    key: str
    phase: str
    title: str = ""
    emoji: str = ""
    archived: bool = False


@dataclass
class Panel:
    key: str
    title: str
    phase: str
    sentences: tuple[str, str]
    lane: int
    downstream: int
    finished_count: int
    waits_on: list[DepRow]
    blocks: list[DepRow]
    finished: list[DepRow]
    spec_set: str


@dataclass
class KeyPanel:
    """Panel data for a dependency key that is not an open ticket."""
    key: str
    kind: str  # missing | finished
    title: str
    archived: bool
    named_by: list[str]


@dataclass
class OutlineRow:
    key: str
    level: int
    trail: tuple[bool, ...]  # per ancestor level: does it continue below this row
    depth: int
    repeat: bool
    cycle: bool
    missing: list[str]
    open_deps: int
    done_deps: int


@dataclass
class DepsView:
    graph: depgraph.DepGraph
    tickets: dict[str, TicketInfo]
    needs_you: list[str]
    waits_on_you: list[str]
    never_starts: list[str]
    waiting: list[str]
    headline: Headline
    start_here: list[StartHere]
    can_start: int
    one_step: int
    stuck_deeper: int
    longest_chain: int
    alerts: list[Alert]
    unlinked: list[str]
    panels: dict[str, Panel]
    key_panels: dict[str, KeyPanel]
    outline: list[OutlineRow]
    finished_links: bool = False
    in_cycle: set[str] = field(default_factory=set)


def _headline(n_open: int, never: int, you: int, waiting: int) -> Headline:
    if not n_open:
        return Headline("The board is", "clear.", "ok")
    if never:
        return Headline(f"{never} can never", "start.", "bad")
    if you:
        return Headline(f"{you} wait on" if you != 1 else "1 waits on", "you.", "you")
    if waiting:
        return Headline(f"{waiting} wait their" if waiting != 1 else "1 waits its", "turn.", "")
    return Headline("Nothing is", "blocked.", "ok")


def _first_sentence(g: depgraph.DepGraph, key: str, in_cycle: bool) -> str:
    node = g.nodes[key]
    if in_cycle:
        return ("Part of a dependency cycle. It cannot start until one dependsOn entry "
                "in the cycle is removed.")
    if node.missing:
        return (f"Depends on {node.missing[0]}, which does not exist. "
                "It cannot start until its spec names a real ticket.")
    opens = node.open_deps
    if len(opens) == 1:
        dep = g.nodes[opens[0]]
        if dep.phase in board.NEEDS_YOU_PHASE_VALUES:
            return f"Waits on {dep.key}, which needs you."
        return f"Waits on {dep.key} ({dep.phase})."
    if opens:
        m = sum(g.nodes[d].phase in board.NEEDS_YOU_PHASE_VALUES for d in opens)
        tail = "" if not m else (", 1 of which needs you" if m == 1 else f", {m} of which need you")
        return f"Waits on {len(opens)} tickets{tail}."
    if node.phase == Phase.DEGRADED.value:
        return "Nothing is in its way, but it is parked after repeated failures. Retry it."
    if node.phase == Phase.AWAITING_HUMAN.value:
        return "Nothing is in its way. It is waiting for your answer."
    return "Nothing is in its way."


def _second_sentence(n: int) -> str:
    if not n:
        return "Nothing waits behind it."
    return "1 ticket waits behind it." if n == 1 else f"{n} tickets wait behind it."


def _spec_set_line(key: str, deps: list[str]) -> str:
    return f"maestro spec-set {key} --depends-on {','.join(deps) if deps else '<KEY>'}"


def outline(g: depgraph.DepGraph) -> list[OutlineRow]:
    """Roots first, each followed depth-first by its dependents; a second visit is a repeat."""
    in_cycle = {k for c in g.cycles for k in c}
    rows: list[OutlineRow] = []
    shown: set[str] = set()

    def row(key: str, level: int, trail: tuple[bool, ...], repeat: bool) -> OutlineRow:
        n = g.nodes[key]
        return OutlineRow(key, level, trail, g.depth[key], repeat, key in in_cycle,
                          list(n.missing), len(n.open_deps), n.done_deps)

    def walk(keys: list[str], level: int, trail: tuple[bool, ...]) -> None:
        for i, key in enumerate(keys):
            more = i < len(keys) - 1
            if key in shown:
                rows.append(row(key, level, trail, True))
                continue
            shown.add(key)
            rows.append(row(key, level, trail, False))
            walk(g.dependents[key], level + 1, trail + (more,))

    walk(list(g.roots), 0, ())
    return rows


def build(home: Path, cfg: Config, now: float | None = None, *,
          finished_links: bool = False) -> DepsView:
    """Fold *home* into one `DepsView`; `finished_links` counts finished deps as links."""
    g = depgraph.build(home)
    now_s = store.now_epoch() if now is None else now
    running = board.running_keys(home, cfg)
    in_cycle = {k for c in g.cycles for k in c}
    order = sorted(g.nodes, key=depgraph._natural)

    down = {k: downstream(g, k) for k in order}
    needs = [k for k in order if g.nodes[k].phase in board.NEEDS_YOU_PHASE_VALUES]
    bad_roots = [k for k in order if k in in_cycle or g.nodes[k].missing]
    never_set = set(bad_roots)
    for k in bad_roots:
        never_set.update(down[k])
    you_set: set[str] = set()
    for k in needs:
        you_set.update(down[k])
    you_set -= never_set
    never = [k for k in order if k in never_set]
    on_you = [k for k in order if k in you_set]
    waiting = [k for k in order if g.nodes[k].blocked]

    tickets = {}
    for k in order:
        n = g.nodes[k]
        snap = snap_mod.load(home, k)
        held = (board.held(home, snap) if now is None
                else fleet_mod.hold_state(home, k, now_s) is not None)
        tickets[k] = TicketInfo(k, n.title, n.phase, group_of(n.phase), held, k in running,
                                g.depth[k], k in in_cycle, len(down[k]))

    start: list[StartHere] = []
    if not never:
        cands = sorted((k for k in needs if down[k]), key=lambda k: -len(down[k]))
        start = [StartHere(k, "Retry" if g.nodes[k].phase == Phase.DEGRADED.value else "Answer",
                           len(down[k])) for k in cands[:START_HERE_MAX]]

    depths = [g.depth[k] for k in order]

    alerts: list[Alert] = []
    if g.cycles:
        alerts.append(Alert("fail", "; ".join(" → ".join(c) for c in g.cycles),
                            sorted({k for c in g.cycles for k in c}, key=depgraph._natural)))
    naming: dict[str, list[str]] = {}
    for k in order:
        for dep in g.nodes[k].missing:
            naming.setdefault(dep, []).append(k)
    for dep, by in naming.items():
        alerts.append(Alert("warn", f"{dep} does not exist, named by {', '.join(by)}", by))

    unlinked = [k for k in order
                if not g.nodes[k].open_deps and not g.dependents[k] and not g.nodes[k].missing
                and not (finished_links and g.nodes[k].done_deps)]

    panels: dict[str, Panel] = {}
    key_panels: dict[str, KeyPanel] = {}
    finished_named: dict[str, list[str]] = {}
    for k in order:
        n = g.nodes[k]
        status = depgraph.dep_status(home, k)
        waits, done = [], []
        for dep, emoji, phase in status:
            if dep in g.nodes:
                waits.append(DepRow(dep, phase, g.nodes[dep].title, emoji))
            elif phase == "missing":
                waits.append(DepRow(dep, phase, "", emoji))
            else:
                done.append(DepRow(dep, phase, _finished_title(home, dep), emoji,
                                   _archived(home, dep)))
                finished_named.setdefault(dep, []).append(k)
        blocks = [DepRow(d, g.nodes[d].phase, g.nodes[d].title) for d in g.dependents[k]]
        panels[k] = Panel(
            k, n.title, n.phase,
            (_first_sentence(g, k, k in in_cycle), _second_sentence(len(down[k]))),
            g.depth[k], len(down[k]), len(done), waits, blocks, done,
            _spec_set_line(k, [d for d, _, _ in status]))
    for dep, by in naming.items():
        key_panels[dep] = KeyPanel(dep, "missing", "", False, by)
    for dep, by in finished_named.items():
        key_panels[dep] = KeyPanel(dep, "finished", _finished_title(home, dep),
                                   _archived(home, dep), by)

    return DepsView(
        graph=g, tickets=tickets, needs_you=needs, waits_on_you=on_you, never_starts=never,
        waiting=waiting, headline=_headline(len(order), len(never), len(on_you), len(waiting)),
        start_here=start,
        can_start=sum(d == 0 for d in depths), one_step=sum(d == 1 for d in depths),
        stuck_deeper=sum(d >= 2 for d in depths),
        longest_chain=(max(depths) + 1) if depths else 0,
        alerts=alerts, unlinked=unlinked, panels=panels, key_panels=key_panels,
        outline=outline(g), finished_links=finished_links, in_cycle=in_cycle)


def _archived(home: Path, key: str) -> bool:
    return (not store.ticket_dir(home, key).exists()
            and (home / "tickets" / "_archive" / key).exists())


def _finished_title(home: Path, key: str) -> str:
    if store.ticket_dir(home, key).exists():
        return snap_mod.display_title(home, snap_mod.load(home, key))
    return ""
