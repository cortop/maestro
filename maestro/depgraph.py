"""Pure dependsOn graph: load the raw edges, build the open-ticket tree + blocking depth."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatcher, snapshot as snap_mod, store
from .statemachine import TERMINAL_PHASES, Phase


def key_exists_anywhere(home: Path, key: str) -> bool:
    """A dependency is only genuinely *missing* if it never existed -- a
    finished, archived ticket (``ops.archive_done``) is a legitimate satisfied
    dependency, not a typo."""
    if store.ticket_dir(home, key).exists():
        return True
    return (home / "tickets" / "_archive" / key).exists()


def load(home: Path) -> dict[str, list[str]]:
    """Every known ticket key -> its spec's dependsOn list (``[]`` without a spec)."""
    graph: dict[str, list[str]] = {}
    for key in dispatcher.list_keys(home):
        spec_file = store.spec_path(home, key)
        graph[key] = (dispatcher.parse_depends_on(spec_file.read_text(encoding="utf-8"))
                      if spec_file.exists() else [])
    return graph


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """DFS cycle detection over the dependsOn graph; each cycle is reported as
    the key path from its first repeated node back to itself."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}
    path: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color.get(dep) == GRAY:
                i = path.index(dep)
                cycles.append(path[i:] + [dep])
            elif color.get(dep) == WHITE:
                visit(dep)
        path.pop()
        color[node] = BLACK

    for node in list(graph):
        if color[node] == WHITE:
            visit(node)
    return cycles


def _natural(key: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", key))


@dataclass
class Node:
    key: str
    title: str
    phase: str
    open_deps: list[str]            # deps that are themselves open, spec order
    done_deps: int = 0              # deps finished (or archived) -- ignored for depth
    missing: list[str] = field(default_factory=list)  # deps that never existed

    @property
    def blocked(self) -> bool:
        return bool(self.open_deps or self.missing)


@dataclass
class DepGraph:
    nodes: dict[str, Node]              # every open ticket
    dependents: dict[str, list[str]]    # open key -> open tickets that depend on it
    roots: list[str]                    # ordered: with dependents first, then the rest
    cycles: list[list[str]]
    depth: dict[str, int]               # longest chain of open deps below each ticket


def build(home: Path) -> DepGraph:
    raw = load(home)
    nodes: dict[str, Node] = {}
    for key, deps in raw.items():
        snap = snap_mod.load(home, key)
        if Phase(snap.phase) in TERMINAL_PHASES:
            continue
        nodes[key] = Node(key, snap_mod.display_title(home, snap), snap.phase, [])
    for key, node in nodes.items():
        for dep in raw[key]:
            if dep in nodes:
                node.open_deps.append(dep)
            elif dep in raw or key_exists_anywhere(home, dep):
                node.done_deps += 1
            else:
                node.missing.append(dep)

    dependents: dict[str, list[str]] = {k: [] for k in nodes}
    for key in sorted(nodes, key=_natural):
        for dep in nodes[key].open_deps:
            dependents[dep].append(key)

    cycles = find_cycles({k: n.open_deps for k, n in nodes.items()})
    in_cycle = {k for c in cycles for k in c}

    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def depth_of(key: str) -> int:
        if key in depth:
            return depth[key]
        visiting.add(key)
        node = nodes[key]
        d = 1 if node.missing else 0
        for dep in node.open_deps:
            if dep not in visiting:
                d = max(d, depth_of(dep) + 1)
        visiting.discard(key)
        if key in in_cycle:
            d = max(d, 2)
        depth[key] = d
        return d

    for key in nodes:
        depth_of(key)

    roots = [k for k in sorted(nodes, key=_natural) if not nodes[k].open_deps or k in in_cycle]
    roots.sort(key=lambda k: not dependents[k])  # stable: dependents first, key order within
    return DepGraph(nodes, dependents, roots, cycles, depth)


def dep_status(home: Path, key: str) -> list[tuple[str, str, str]]:
    """Each of *key*'s spec dependsOn entries as ``(dep, emoji, phase)``, spec order.

    ✅ terminal · 💤 triaging/ready · ⏳ any other open phase · ❓ never existed
    (phase ``missing``). Read-only; an archived dep counts as its archived phase."""
    spec_file = store.spec_path(home, key)
    deps = (dispatcher.parse_depends_on(spec_file.read_text(encoding="utf-8"))
            if spec_file.exists() else [])
    out: list[tuple[str, str, str]] = []
    for dep in deps:
        if not key_exists_anywhere(home, dep):
            out.append((dep, "❓", "missing"))
            continue
        phase = Phase(snap_mod.load(home, dep).phase)
        if phase in TERMINAL_PHASES:
            emoji = "✅"
        elif phase in (Phase.TRIAGING, Phase.READY):
            emoji = "💤"
        else:
            emoji = "⏳"
        out.append((dep, emoji, phase.value))
    return out
