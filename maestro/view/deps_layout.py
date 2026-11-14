"""Layered left-to-right layout of a `DepGraph`: card boxes, lane boxes and SVG path strings."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..depgraph import DepGraph, _natural

KEEP = 4
FAN_MAX = 6
PADL, PADR, PADT, PADB = 40, 40, 18, 24
ARROW = 8


@dataclass(frozen=True)
class Geometry:
    nw: int      # card width
    nh: int      # card height
    cg: int      # lane gap
    rg: int      # row gap
    dh: int      # long-edge slot height
    band: int    # gap between groups
    gh: int      # ghost / fold card height


DETAILED = Geometry(nw=216, nh=68, cg=76, rg=14, dh=14, band=30, gh=30)
COMPACT = Geometry(nw=176, nh=34, cg=48, rg=10, dh=10, band=22, gh=28)


@dataclass
class Card:
    """One box on the stage. kind: ticket | missing | done | stack | fold."""
    id: str
    kind: str
    key: str
    x: float
    y: float
    w: float
    h: float
    lane: int
    parent: str | None = None          # stack / fold: the ticket they belong to
    keys: list[str] = field(default_factory=list)   # stack / fold: tickets they stand for
    to: list[str] = field(default_factory=list)     # done ghost: open tickets naming it


@dataclass
class Edge:
    source: str
    target: str
    kind: str          # open | cycle | missing | done | fold
    d: str             # SVG path of the curve
    head: str          # SVG path of the arrowhead
    mid: tuple[float, float] | None
    back: bool = False


@dataclass
class Lane:
    index: int
    x: float
    w: float
    count: int


@dataclass
class Layout:
    geometry: Geometry
    width: float
    height: float
    cards: list[Card]       # tickets
    ghosts: list[Card]      # missing and finished dependencies
    stacks: list[Card]      # stack and fold cards
    edges: list[Edge]
    lanes: list[Lane]
    lane_cards: list[list[str]]   # per lane, ticket keys top to bottom
    unlinked: list[str]
    lane0: int              # 1 when the finished-dependency lane is present
    hidden: dict[str, str]  # ticket key hidden in a stack -> its parent


def _nat(k: str) -> tuple:
    return _natural(k)


def _jsround(x: float) -> int:
    return math.floor(x + 0.5)


def _n(x: float) -> str:
    x = round(x, 3)
    return str(int(x)) if x == int(x) else repr(x)


def _head(x: float, y: float) -> str:
    return (f"M {_n(x)} {_n(y)} L {_n(x - ARROW)} {_n(y - ARROW / 2)} "
            f"L {_n(x - ARROW)} {_n(y + ARROW / 2)} Z")


def place(want: list[float], sep: list[float]) -> list[float]:
    """Order-preserving centres as close to *want* as the minimum gaps allow."""
    n = len(want)
    if not n:
        return []
    off = [0.0]
    for i in range(1, n):
        off.append(off[i - 1] + sep[i - 1])
    blocks: list[list[float]] = []   # [sum, count]
    for i in range(n):
        b = [want[i] - off[i], 1]
        while blocks and blocks[-1][0] / blocks[-1][1] > b[0] / b[1]:
            p = blocks.pop()
            b = [p[0] + b[0], p[1] + b[1]]
        blocks.append(b)
    out: list[float] = []
    for s, cnt in blocks:
        v = s / cnt
        for _ in range(int(cnt)):
            out.append(v + off[len(out)])
    return out


class _Item:
    __slots__ = ("id", "type", "key", "h", "parent", "keys")

    def __init__(self, id, type, key, h, parent=None, keys=()):
        self.id, self.type, self.key, self.h = id, type, key, h
        self.parent, self.keys = parent, list(keys)


class _Edge:
    __slots__ = ("s", "t", "kind", "back", "chain", "by")

    def __init__(self, s, t, kind, back):
        self.s, self.t, self.kind, self.back = s, t, kind, back
        self.chain: list = []
        self.by = 0


class _Ent:
    __slots__ = ("id", "h", "dummy", "l", "pred", "succ", "o", "i", "b", "c")

    def __init__(self, id, h, dummy=False):
        self.id, self.h, self.dummy = id, h, dummy
        self.l = 0
        self.pred: list[_Ent] = []
        self.succ: list[_Ent] = []
        self.o = self.i = 0
        self.b = self.c = 0.0


def _closure(start: str, nxt) -> set[str]:
    out: set[str] = set()
    stack = [start]
    while stack:
        for x in nxt(stack.pop()):
            if x not in out:
                out.add(x)
                stack.append(x)
    out.discard(start)
    return out


def layout(graph: DepGraph, *, finished: bool = False, compact: bool = False,
           only: str | None = None, expanded=(), selected: str | None = None) -> Layout:
    G = COMPACT if compact else DETAILED
    nodes = graph.nodes
    order = list(nodes)
    expanded = set(expanded)

    cyc_edge: set[tuple[str, str]] = set()      # (dependency, dependent)
    back_edge: set[tuple[str, str]] = set()
    for c in graph.cycles:
        for i in range(len(c) - 1):
            cyc_edge.add((c[i + 1], c[i]))
        back_edge.add((c[-1], c[-2]))

    vis = order
    if only is not None and only in nodes:
        keep = {only}
        keep |= _closure(only, lambda x: nodes[x].open_deps)
        keep |= _closure(only, lambda x: graph.dependents[x])
        vis = [k for k in order if k in keep]
    in_vis = set(vis)

    def o_deps(k):
        return [d for d in nodes[k].open_deps if d in in_vis]

    def o_down(k):
        return [d for d in graph.dependents[k] if d in in_vis]

    linked = [k for k in vis if o_deps(k) or o_down(k) or nodes[k].missing
              or (finished and nodes[k].done_keys) or k == only]
    linked_set = set(linked)
    unlinked = [] if only is not None else [k for k in vis if k not in linked_set]

    # A parent with many leaf-only dependents shows the first few and stacks the rest.
    hidden: dict[str, str] = {}
    stacks: list[_Item] = []
    for p in sorted(linked, key=_nat):
        leaves = [k for k in o_down(p)
                  if o_deps(k) == [p] and not o_down(k) and not nodes[k].missing
                  and not (finished and nodes[k].done_keys)]
        if len(leaves) <= FAN_MAX:
            continue
        sel_at = leaves.index(selected) if selected in leaves else -1
        if p in expanded or sel_at >= KEEP:
            stacks.append(_Item("fold:" + p, "fold", p, G.gh, p, leaves))
        else:
            rest = leaves[KEEP:]
            for k in rest:
                hidden[k] = p
            stacks.append(_Item("stack:" + p, "stack", p, G.nh, p, rest))

    items: dict[str, _Item] = {}
    ids: list[str] = []

    def add(it: _Item) -> None:
        items[it.id] = it
        ids.append(it.id)

    for k in linked:
        if k not in hidden:
            add(_Item(k, "t", k, G.nh))
    for k in linked:
        if k not in hidden:
            for m in nodes[k].missing:
                if "?" + m not in items:
                    add(_Item("?" + m, "missing", m, G.nh))
    for s in stacks:
        add(s)

    edges: list[_Edge] = []
    for id_ in ids:
        if items[id_].type != "t":
            continue
        for d in o_deps(id_):
            if d not in items:
                continue
            ek = (d, id_)
            edges.append(_Edge(d, id_, "cycle" if ek in cyc_edge else "open", ek in back_edge))
        for m in nodes[id_].missing:
            edges.append(_Edge("?" + m, id_, "missing", False))
    for s in stacks:
        edges.append(_Edge(s.parent, s.id, "open" if s.type == "stack" else "fold", False))

    in_e: dict[str, list[_Edge]] = {i: [] for i in ids}
    out_e: dict[str, list[_Edge]] = {i: [] for i in ids}
    for e in edges:
        if not e.back:
            in_e[e.t].append(e)
            out_e[e.s].append(e)
    lay: dict[str, int] = {}
    busy: set[str] = set()

    def layer_of(id_: str) -> int:
        if id_ in lay:
            return lay[id_]
        if id_ in busy:
            return 0
        busy.add(id_)
        l = 0
        for e in in_e[id_]:
            l = max(l, layer_of(e.s) + 1)
        lay[id_] = l
        return l

    for id_ in ids:
        layer_of(id_)
    for id_ in ids:
        if items[id_].type == "missing":
            lay[id_] = max(0, min(lay[e.t] for e in out_e[id_]) - 1)

    par = {i: i for i in ids}

    def find(x: str) -> str:
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    for e in edges:
        par[find(e.s)] = find(e.t)
    groups: dict[str, list[str]] = {}
    for id_ in ids:
        groups.setdefault(find(id_), []).append(id_)
    bands = []
    for m in groups.values():
        real = sorted((x for x in m if items[x].type == "t"), key=_nat)
        size = sum(len(items[x].keys) if items[x].type == "stack"
                   else 1 if items[x].type == "t" else 0 for x in m)
        bands.append((m, real[0] if real else m[0], size))
    bands.sort(key=lambda b: (-b[2], _nat(b[1])))

    pos: dict[str, tuple[int, int, int]] = {}      # id -> (lane, cy, h)
    max_layer = -1
    y_off = PADT
    for band_ids, _min, _size in bands:
        in_band = set(band_ids)
        L: list[list[_Ent]] = []
        ent: dict[str, _Ent] = {}

        def put(l: int, e: _Ent) -> None:
            while len(L) <= l:
                L.append([])
            L[l].append(e)
            ent[e.id] = e
            e.l = l

        for id_ in band_ids:
            put(lay[id_], _Ent(id_, items[id_].h))
        for e in edges:
            if e.back or e.s not in in_band:
                continue
            prev = ent[e.s]
            chain = []
            for l in range(lay[e.s] + 1, lay[e.t]):
                d = _Ent(f"~{e.s}>{e.t}:{l}", G.dh, True)
                put(l, d)
                chain.append(d)
                prev.succ.append(d)
                d.pred.append(prev)
                prev = d
            prev.succ.append(ent[e.t])
            ent[e.t].pred.append(prev)
            e.chain = chain

        # order: depth-first seed, then barycentre sweeps
        counter = 0
        seen: set[str] = set()

        def dfs(e: _Ent) -> None:
            nonlocal counter
            if e.id in seen:
                return
            seen.add(e.id)
            e.o = counter
            counter += 1
            for s in sorted(e.succ, key=lambda x: _nat(x.id)):
                dfs(s)

        for layer in L:
            for e in sorted(layer, key=lambda x: _nat(x.id)):
                dfs(e)
        for layer in L:
            layer.sort(key=lambda x: x.o)
            for i, e in enumerate(layer):
                e.i = i

        def sweep(layer: list[_Ent], side: str) -> None:
            for e in layer:
                ns = getattr(e, side)
                e.b = sum(x.i for x in ns) / len(ns) if ns else e.i
            layer.sort(key=lambda x: (x.b, x.i))
            for i, e in enumerate(layer):
                e.i = i

        for _ in range(4):
            for l in range(1, len(L)):
                sweep(L[l], "pred")
            for l in range(len(L) - 2, -1, -1):
                sweep(L[l], "succ")

        # coordinates: stack each lane, then pull every card towards the cards it is wired to
        def sep_of(layer: list[_Ent]) -> list[float]:
            return [(e.h + layer[i + 1].h) / 2 + (6 if e.dummy and layer[i + 1].dummy else G.rg)
                    for i, e in enumerate(layer[:-1])]

        tall = 0.0
        spans: list[float] = []
        for layer in L:
            y = 0.0
            sp = sep_of(layer)
            for i, e in enumerate(layer):
                e.c = y
                y += sp[i] if i < len(sp) else 0
            span = layer[-1].c if layer else 0.0
            spans.append(span)
            tall = max(tall, span)
        for layer, span in zip(L, spans):
            sh = (tall - span) / 2
            for e in layer:
                e.c += sh

        def relax(layer: list[_Ent], pick) -> None:
            if not layer:
                return
            want = []
            for e in layer:
                ns = pick(e)
                want.append(sum(x.c for x in ns) / len(ns) if ns else e.c)
            out = place(want, sep_of(layer))
            for e, c in zip(layer, out):
                e.c = c

        for _ in range(3):
            for l in range(1, len(L)):
                relax(L[l], lambda e: e.pred)
            for l in range(len(L) - 2, -1, -1):
                relax(L[l], lambda e: e.succ)
        for l in range(len(L)):
            relax(L[l], lambda e: e.pred + e.succ)

        top, bot = math.inf, -math.inf
        for layer in L:
            for e in layer:
                top = min(top, e.c - e.h / 2)
                bot = max(bot, e.c + e.h / 2)
        for layer in L:
            for e in layer:
                pos[e.id] = (e.l, _jsround(e.c - top + y_off), e.h)
        band_h = bot - top
        loop = 0
        for e in edges:
            if not e.back or e.s not in in_band:
                continue
            lo, hi = min(lay[e.s], lay[e.t]), max(lay[e.s], lay[e.t])
            b = 0.0
            for l in range(lo, hi + 1):
                for x in L[l]:
                    b = max(b, x.c + x.h / 2 - top)
            by = b + 18 + loop * 10
            loop += 1
            e.by = _jsround(by + y_off)
            band_h = max(band_h, by + 8)
        max_layer = max(max_layer, len(L) - 1)
        y_off += _jsround(band_h) + G.band
    height: float = y_off - G.band + PADB if ids else 0

    # finished dependencies live in their own lane at the far left
    done_ghosts: list[Card] = []
    lane0 = 0
    if finished:
        refs: dict[str, list[str]] = {}
        for id_ in ids:
            if items[id_].type == "t":
                for d in nodes[id_].done_keys:
                    refs.setdefault(d, []).append(id_)
        if refs:
            lane0 = 1
            arr = [(sum(pos[i][1] for i in to) / len(to), k, to) for k, to in refs.items()]
            arr.sort(key=lambda a: (a[0], _nat(a[1])))
            out = place([a[0] for a in arr], [G.gh + 8] * (len(arr) - 1))
            min_c = PADT + G.gh / 2
            shift = min_c - out[0] if out[0] < min_c else 0
            for (want, k, to), c in zip(arr, out):
                cy = _jsround(c + shift)
                done_ghosts.append(Card("done:" + k, "done", k, PADL, cy - G.gh / 2,
                                        G.nw, G.gh, -1, to=to))
            last = done_ghosts[-1]
            height = max(height, last.y + G.gh + PADB)

    pitch = G.nw + G.cg

    def lx(l: int) -> int:
        return PADL + (l + lane0) * pitch

    width = PADL + (max_layer + 1 + lane0) * pitch - G.cg + PADR if ids else 0

    cards: list[Card] = []
    ghosts: list[Card] = []
    stack_cards: list[Card] = []
    for id_ in ids:
        it = items[id_]
        l, cy, h = pos[id_]
        card = Card(id_, {"t": "ticket"}.get(it.type, it.type), it.key,
                    lx(l), cy - h / 2, G.nw, h, l, it.parent, list(it.keys))
        {"t": cards, "missing": ghosts}.get(it.type, stack_cards).append(card)

    drawn: list[Edge] = []
    for e in edges:
        sl, scy, _ = pos[e.s]
        tl, tcy, _ = pos[e.t]
        sx, tx = lx(sl) + G.nw, lx(tl) - 1
        mid = None
        if e.back:
            r, by = 33, e.by
            d = (f"M {sx} {scy} C {sx + r} {scy}, {sx + r} {by}, {sx} {by} L {tx} {by} "
                 f"C {tx - r} {by}, {tx - r} {tcy}, {tx - 7} {tcy}")
        else:
            px, py = sx, scy
            parts = [f"M {sx} {scy}"]

            def seg(x, y):
                nonlocal px, py, mid
                k = max(18, (x - px) * 0.5)
                parts.append(f"C {_n(px + k)} {py}, {_n(x - k)} {y}, {x} {y}")
                if mid is None:
                    mid = ((px + x) / 2, (py + y) / 2)
                px, py = x, y

            for dm in e.chain:
                dl, dcy, _ = pos[dm.id]
                x0 = lx(dl)
                seg(x0, dcy)
                parts.append(f"L {x0 + G.nw} {dcy}")
                px = x0 + G.nw
            seg(tx - 7, tcy)
            d = " ".join(parts)
        drawn.append(Edge(e.s, e.t, e.kind, d, _head(tx, tcy), mid, e.back))
    for g in done_ghosts:
        gcy = g.y + G.gh / 2
        for id_ in g.to:
            tl, tcy, _ = pos[id_]
            sx, tx = PADL + G.nw, lx(tl) - 1
            k = min(160, max(30, (tx - sx) * 0.4))
            drawn.append(Edge(g.id, id_, "done",
                              f"M {sx} {_n(gcy)} C {_n(sx + k)} {_n(gcy)}, {_n(tx - 7 - k)} {tcy}, "
                              f"{tx - 7} {tcy}", _head(tx, tcy), None))
    ghosts = done_ghosts + ghosts

    lanes = [Lane(l, lx(l) - G.cg / 2, pitch, 0) for l in range(max_layer + 1)]
    for c in cards:
        lanes[c.lane].count += 1
    for c in stack_cards:
        if c.kind == "stack":
            lanes[c.lane].count += len(c.keys)
    lane_cards: list[list[str]] = [[] for _ in lanes]
    for c in sorted(cards, key=lambda c: c.y):
        lane_cards[c.lane].append(c.key)

    return Layout(G, width, height, cards, ghosts, stack_cards, drawn, lanes, lane_cards,
                  unlinked, lane0, hidden)
