"""Dependency page routes (`/deps`, `/deps/headline`, `/deps/main`) of `maestro web`."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup, escape

from ..view import deps as deps_view, deps_layout
from .shell import Shell

TRAY_MAX = 18
TRAY_SHOWN = 17
FRAME_MIN, FRAME_MAX, FRAME_EMPTY = 300, 640, 280
LANE_HEADER = 34
LANE_LABELS = ("can start", "one step away")


class Params:
    """The optional query parameters of the page; `url` rebuilds a link that keeps them."""

    def __init__(self, view: str = "graph", finished: str = "", compact: str = "", tray: str = "",
                 select: str = "", only: str = "", open: str = "", mark: str = ""):
        self.view = "outline" if view == "outline" else "graph"
        self.finished = finished == "1"
        self.compact = compact == "1"
        self.tray_all = tray == "all"
        self.select = select
        self.only = only
        self.open = tuple(k for k in dict.fromkeys(x.strip() for x in open.split(",")) if k)
        self.mark = mark

    def query(self, **over) -> str:
        """The query string with defaults omitted; *over* replaces individual parameters."""
        cur = {"view": self.view, "finished": self.finished, "compact": self.compact,
               "tray": "all" if self.tray_all else "", "select": self.select, "only": self.only,
               "open": self.open, "mark": self.mark}
        cur.update(over)
        out = {}
        if cur["view"] != "graph":
            out["view"] = cur["view"]
        if cur["finished"]:
            out["finished"] = "1"
        if cur["compact"]:
            out["compact"] = "1"
        if cur["tray"] == "all":
            out["tray"] = "all"
        for name in ("select", "only"):
            if cur[name]:
                out[name] = cur[name]
        if cur["open"]:
            out["open"] = ",".join(cur["open"])
        if cur["mark"]:
            out["mark"] = cur["mark"]
        return urlencode(out, safe=",:")

    def url(self, path: str = "/deps", **over) -> str:
        q = self.query(**over)
        return f"{path}?{q}" if q else path

    def pin(self, id_: str) -> str:
        """Link target that pins *id_*; the pinned one clears the pin instead."""
        return self.url(select="" if id_ == self.select else id_)

    def link(self, href: str) -> Markup:
        """`href` plus the htmx attributes that swap the page content and push the URL."""
        h = escape(href)
        return Markup(f'href="{h}" hx-get="{h}" hx-target="#deps-page" hx-select="#deps-page" '
                      f'hx-swap="outerHTML" hx-push-url="true"')


def _lede(v: deps_view.DepsView) -> str:
    if not v.tickets:
        return "No open tickets, so nothing can be waiting."
    if v.never_starts:
        return ("A cycle or a dependency that does not exist. Nothing behind it will ever be due "
                "until a spec is edited.")
    if v.waits_on_you:
        return "Each ticket below is waiting for you and has others queued behind it."
    if v.waiting:
        return "Nothing here needs you. Every blocker is moving on its own."
    return "No open ticket depends on another. Each one starts as soon as a session is free."


def headline_ctx(v: deps_view.DepsView, p: Params) -> dict:
    """Hero region: kicker numbers, headline, lede, start-here chips and the four counters."""
    g = v.graph
    n_open = len(v.tickets)
    counters = (("Can start", v.can_start), ("One step away", v.one_step),
                ("Stuck deeper", v.stuck_deeper), ("Longest chain", v.longest_chain))
    return {"dv": v, "p": p, "n_open": n_open, "n_linked": n_open - len(v.unlinked),
            "n_deps": sum(len(n.open_deps) for n in g.nodes.values()),
            "lede": _lede(v), "counters": counters, "query": p.query()}


def _alerts(v: deps_view.DepsView, p: Params) -> list[dict]:
    out = []
    for a in v.alerts:
        if a.level == "fail":
            cycles = v.graph.cycles
            out.append({"cls": "dep-alert", "tag": f"{len(cycles)} cycle{'s' if len(cycles) != 1 else ''}",
                        "text": " · ".join(" → ".join(c) for c in cycles),
                        "line": "Nothing in a cycle can start. Remove one dependsOn entry to break it.",
                        "href": p.url(mark="cycle")})
        else:
            by = ", ".join(a.keys)
            verb = "depends" if len(a.keys) == 1 else "depend"
            out.append({"cls": "dep-alert warn", "tag": "missing",
                        "text": f"{by} {verb} on {a.subject}, which does not exist",
                        "line": ("Blocked until the spec names a real ticket: "
                                 f"maestro spec-set {a.keys[0]} --depends-on <KEY>"),
                        "href": p.url(mark=f"missing:{a.subject}")})
    return out


def resolve_pin(v: deps_view.DepsView, select: str) -> tuple[str, str] | None:
    """(kind, key) of the pinned thing -- ticket, missing or done ghost -- or None."""
    if select in v.tickets:
        return "ticket", select
    if select.startswith("?") and v.key_panels.get(select[1:], None) and \
            v.key_panels[select[1:]].kind == "missing":
        return "missing", select[1:]
    if select.startswith("done:") and v.key_panels.get(select[5:], None) and \
            v.key_panels[select[5:]].kind == "finished":
        return "done", select[5:]
    return None


def _ghosts_of(v: deps_view.DepsView, keys) -> set[str]:
    g = v.graph
    out: set[str] = set()
    for k in keys:
        out.update("?" + m for m in g.nodes[k].missing)
        out.update("done:" + d for d in g.nodes[k].done_keys)
    return out


def classify(v: deps_view.DepsView, lay, p: Params, pin) -> tuple[dict[str, str], dict[int, str]]:
    """Per card id its `is-*` class, and per edge index `lit-up`, `lit-down` or `is-dim`."""
    g = v.graph
    ids = ([c.id for c in lay.cards] + [c.id for c in lay.ghosts] + [c.id for c in lay.stacks]
           + list(v.unlinked))
    state: dict[str, str] = {}
    lit: dict[int, str] = {}
    if pin:
        kind, key = pin
        pid = key if kind == "ticket" else ("?" if kind == "missing" else "done:") + key
        up: set[str] = set()
        down: set[str] = set()
        if kind == "ticket":
            ups = deps_view.upstream(g, key)
            up = set(ups) | _ghosts_of(v, [key, *ups])
            down = set(deps_view.downstream(g, key))
        else:
            named = v.key_panels[key].named_by
            down = set(named)
            if kind == "missing":
                for k in named:
                    down.update(deps_view.downstream(g, k))
        if kind != "done":
            for c in lay.stacks:
                if c.parent == pid or c.parent in down:
                    down.add(c.id)
        for i in ids:
            state[i] = "is-sel" if i == pid else "" if i in up or i in down else "is-dim"
        for n, e in enumerate(lay.edges):
            if e.target in (up | {pid}) and e.source in up:
                lit[n] = "lit-up"
            elif e.source in (down | {pid}) and e.target in down:
                lit[n] = "lit-down"
            else:
                lit[n] = "is-dim"
    elif p.mark == "cycle" or p.mark.startswith("missing:"):
        if p.mark == "cycle":
            match = set(v.in_cycle)
        else:
            subject = p.mark[len("missing:"):]
            kp = v.key_panels.get(subject)
            match = set(kp.named_by) | {"?" + subject} if kp and kp.kind == "missing" else set()
        for i in ids:
            state[i] = "is-match" if i in match else "is-dim"
        for n, e in enumerate(lay.edges):
            lit[n] = "" if e.source in match and e.target in match else "is-dim"
    return state, lit


def panel_ctx(v: deps_view.DepsView, p: Params, pin) -> dict | None:
    """Side panel data of the pinned ticket or ghost, rows already carrying their links."""
    if not pin:
        return None
    kind, key = pin

    def row(r, cls: str, sel: str) -> dict:
        label = "No such ticket" if cls == "s-missing" else r.phase
        return {"key": r.key, "cls": cls, "phase": r.phase, "chip_label": label,
                "title": r.title, "href": p.url(select=sel)}

    base = {"key": key, "kind": kind, "close": p.url(select=""),
            "open_href": f"/t/{key}",
            "only_label": "Show everything" if p.only else "Only this chain",
            "only_href": p.url(only="" if p.only else key)}
    if kind != "ticket":
        kp = v.key_panels[key]
        rows = [row(deps_view.DepRow(k, v.tickets[k].phase, v.tickets[k].title), "s-open", k)
                for k in kp.named_by]
        return {**base, "ghost": True, "title": "No ticket has this key." if kind == "missing"
                else kp.title, "chip": "No such ticket" if kind == "missing" else "Done",
                "group": f"{'Named by' if kind == 'missing' else 'Was needed by'} \u00b7 {len(rows)}",
                "rows": rows}
    pn = v.panels[key]
    t = v.tickets[key]
    lane = ("can start" if pn.lane == 0 else "one step away" if pn.lane == 1
            else "behind a chain")
    groups = []
    waits = [row(r, "s-missing" if r.phase == "missing" else "s-open",
                 "?" + r.key if r.phase == "missing" else r.key) for r in pn.waits_on]
    for label, cls, rows in (
            ("Waits on", "dep-group g-up", waits),
            ("Blocks", "dep-group", [row(r, "s-open", r.key) for r in pn.blocks]),
            ("Finished", "dep-group", [row(r, "s-done", "done:" + r.key) for r in pn.finished])):
        if rows:
            groups.append({"label": f"{label} \u00b7 {len(rows)}", "cls": cls, "rows": rows})
    return {**base, "ghost": False, "title": pn.title or "(no title)", "phase": pn.phase,
            "held": t.held, "sentences": pn.sentences,
            "facts": (("Lane", f"{pn.lane} \u00b7 {lane}"), ("Holds up", pn.downstream),
                      ("Deps done", pn.finished_count)),
            "groups": groups, "cmd": pn.spec_set}


def graph_ctx(v: deps_view.DepsView, p: Params) -> dict:
    """Main region minus the outline: alerts, lanes, stage boxes, legend and the unlinked tray."""
    pin = resolve_pin(v, p.select)
    lay = deps_layout.layout(v.graph, finished=p.finished, compact=p.compact,
                             only=p.only or None, expanded=p.open,
                             selected=pin[1] if pin and pin[0] == "ticket" else None)
    g = lay.geometry
    drawn = bool(lay.cards)
    frame_h = FRAME_EMPTY
    stage_h = lay.height
    if drawn:
        frame_h = max(FRAME_MIN, min(FRAME_MAX, int(lay.height) + LANE_HEADER + 2))
        stage_h = max(lay.height, frame_h - 2 - LANE_HEADER)
    lanes = []
    first_x = lay.lanes[0].x if lay.lanes else 0
    done_ghosts = [c for c in lay.ghosts if c.kind == "done"]
    if lay.lane0:
        first_x = deps_layout.PADL - g.cg / 2
        lanes.append({"x": first_x, "w": g.nw + g.cg, "done": True, "n": "", "label": "finished",
                      "count": len(done_ghosts)})
    for ln in lay.lanes:
        label = LANE_LABELS[ln.index] if ln.index < len(LANE_LABELS) else "behind a chain"
        lanes.append({"x": ln.x, "w": ln.w, "done": False, "n": ln.index, "label": label,
                      "count": ln.count})
    finished = {k: kp for k, kp in v.key_panels.items() if kp.kind == "finished"}
    unlinked = v.unlinked
    tray_more = len(unlinked) > TRAY_MAX and not p.tray_all
    state, lit = classify(v, lay, p, pin)
    edges = [(e, lit.get(i, "")) for i, e in enumerate(lay.edges)]
    edges.sort(key=lambda t: t[1] in ("lit-up", "lit-down"))
    return {
        "dv": v, "p": p, "lay": lay, "state": state, "edges": edges,
        "panel": panel_ctx(v, p, pin), "only": p.only, "drawn": drawn, "frame_h": frame_h, "stage_h": stage_h,
        "lanes": lanes, "bg_w": g.nw + g.cg, "bg_x": first_x, "alerts": _alerts(v, p),
        "archived": {k: kp.archived for k, kp in finished.items()},
        "empty": ("No open tickets." if not v.tickets else "Nothing is waiting on another ticket.",
                  "When tickets are filed, the ones that wait on each other show up here."
                  if not v.tickets else
                  "Dependencies appear here as soon as a spec names one in dependsOn."),
        "tray": unlinked[:TRAY_SHOWN] if tray_more else unlinked, "tray_more": tray_more,
        "n_finished": len(finished), "compact": p.compact,
        "tray_all_url": p.url(tray="all"),
    }


def outline_ctx(v: deps_view.DepsView) -> dict:
    """The outline table: one line per open ticket, dependents under their blocker."""
    lines = [{"key": r.key, "title": v.tickets[r.key].title, "phase": v.tickets[r.key].phase,
              "level": r.level, "depth": r.depth, "repeat": r.repeat, "cycle": r.cycle,
              "missing": r.missing} for r in v.outline]
    return {"lines": lines}


def register(app: FastAPI, shell: Shell) -> None:
    def load(request: Request):
        q = request.query_params
        p = Params(**{k: q.get(k, "") for k in ("view", "finished", "compact", "tray", "select",
                                                 "only", "open", "mark")})
        return deps_view.build(shell.home, shell.cfg(), finished_links=p.finished), p

    @app.get("/deps", response_class=HTMLResponse)
    def deps(request: Request):
        v, p = load(request)
        return shell.render(request, "deps.html", nav="tickets",
                            **{**headline_ctx(v, p), **graph_ctx(v, p), **outline_ctx(v)})

    @app.get("/deps/headline", response_class=HTMLResponse)
    def headline(request: Request):
        v, p = load(request)
        return shell.render(request, "_deps_headline.html", **headline_ctx(v, p))

    @app.get("/deps/main", response_class=HTMLResponse)
    def main(request: Request):
        v, p = load(request)
        return shell.render(request, "_deps_main.html", **graph_ctx(v, p), **outline_ctx(v))
