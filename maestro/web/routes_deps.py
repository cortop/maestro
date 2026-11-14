"""Dependency page routes (`/deps`, `/deps/headline`, `/deps/main`) of `maestro web`."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ..view import deps as deps_view, deps_layout
from .shell import Shell

TRAY_MAX = 18
TRAY_SHOWN = 17
FRAME_MIN, FRAME_MAX, FRAME_EMPTY = 300, 640, 280
LANE_HEADER = 34
LANE_LABELS = ("can start", "one step away")


class Params:
    """The optional query parameters of the page; `url` rebuilds a link that keeps them."""

    def __init__(self, view: str = "graph", finished: str = "", compact: str = "", tray: str = ""):
        self.view = "outline" if view == "outline" else "graph"
        self.finished = finished == "1"
        self.compact = compact == "1"
        self.tray_all = tray == "all"

    def query(self, **over) -> str:
        """The query string with defaults omitted; *over* replaces individual parameters."""
        cur = {"view": self.view, "finished": self.finished, "compact": self.compact,
               "tray": "all" if self.tray_all else ""}
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
        return urlencode(out)

    def url(self, path: str = "/deps", **over) -> str:
        q = self.query(**over)
        return f"{path}?{q}" if q else path


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


def _alerts(v: deps_view.DepsView) -> list[dict]:
    out = []
    for a in v.alerts:
        if a.level == "fail":
            cycles = v.graph.cycles
            out.append({"cls": "dep-alert", "tag": f"{len(cycles)} cycle{'s' if len(cycles) != 1 else ''}",
                        "text": " · ".join(" → ".join(c) for c in cycles),
                        "line": "Nothing in a cycle can start. Remove one dependsOn entry to break it."})
        else:
            by = ", ".join(a.keys)
            verb = "depends" if len(a.keys) == 1 else "depend"
            out.append({"cls": "dep-alert warn", "tag": "missing",
                        "text": f"{by} {verb} on {a.subject}, which does not exist",
                        "line": ("Blocked until the spec names a real ticket: "
                                 f"maestro spec-set {a.keys[0]} --depends-on <KEY>")})
    return out


def graph_ctx(v: deps_view.DepsView, p: Params) -> dict:
    """Main region minus the outline: alerts, lanes, stage boxes, legend and the unlinked tray."""
    lay = deps_layout.layout(v.graph, finished=p.finished, compact=p.compact)
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
    return {
        "dv": v, "p": p, "lay": lay, "drawn": drawn, "frame_h": frame_h, "stage_h": stage_h,
        "lanes": lanes, "bg_w": g.nw + g.cg, "bg_x": first_x, "alerts": _alerts(v),
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
    def load(view: str, finished: str, compact: str, tray: str):
        p = Params(view, finished, compact, tray)
        return deps_view.build(shell.home, shell.cfg(), finished_links=p.finished), p

    @app.get("/deps", response_class=HTMLResponse)
    def deps(request: Request, view: str = "graph", finished: str = "", compact: str = "",
             tray: str = ""):
        v, p = load(view, finished, compact, tray)
        return shell.render(request, "deps.html", nav="tickets",
                            **{**headline_ctx(v, p), **graph_ctx(v, p), **outline_ctx(v)})

    @app.get("/deps/headline", response_class=HTMLResponse)
    def headline(request: Request, view: str = "graph", finished: str = "", compact: str = "",
                 tray: str = ""):
        v, p = load(view, finished, compact, tray)
        return shell.render(request, "_deps_headline.html", **headline_ctx(v, p))

    @app.get("/deps/main", response_class=HTMLResponse)
    def main(request: Request, view: str = "graph", finished: str = "", compact: str = "",
             tray: str = ""):
        v, p = load(view, finished, compact, tray)
        return shell.render(request, "_deps_main.html", **graph_ctx(v, p), **outline_ctx(v))
