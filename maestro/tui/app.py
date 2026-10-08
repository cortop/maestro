"""The MaestroTUI app: main board table, key actions, and the `maestro tui` entrypoint."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static
from textual.worker import Worker, WorkerState

from rich.text import Text

from .. import claims, ratelimit, spend as spend_mod, config as config_mod, depgraph, event_log, fleet as fleet_mod, health, inbox, ops as ops_mod, snapshot as snap_mod, store
from ..config import Config
from ..dispatcher import existing_prefixes, spec_runner
from .. import dispatcher as disp
from ..projection import _PHASE_RANK, phase_predicate, ticket_rows
from ..sessions import build_routing_sessions, reap_children
from ..statemachine import Phase, ACTIVE_PHASES
from .detail import render as _render_detail
from .events import render_log
from .modals import (
    _ACCEPT_ALL, _AcceptedRecommendation, _AddAcModal, _AnswerModal, _CmdModal, _ConfirmModal,
    HoldModal, _CreateModal, _ImportLinearModal, _InboxModal, _RunnerModal, _SuggestAcsModal,
)
from .render import _dep_color, _nudge_toast, _render_badge, _render_pulse, _styled_row
from .screens import (
    DetailScreen,
    ActivityScreen,
    DepsScreen,
    EnvScreen,
    EventsScreen,
    FleetScreen,
    InboxScreen,
    LogsScreen,
    ProposalScreen,
    ScheduleScreen,
    SpecScreen,
    AcScreen,
    edit_in_editor,
)

_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})
_NEEDS_YOU_PHASE_VALUES = {p.value for p in _NEEDS_YOU_PHASES}


def _needs_you_predicate(home: Path, s: snap_mod.Snapshot) -> bool:
    """The needs-you filter: the two sleeping-and-stuck phases."""
    del home
    return s.phase in _NEEDS_YOU_PHASE_VALUES


def _running_placeholder(home: Path, s: snap_mod.Snapshot) -> bool:
    """Stand-in for the `running` filter: the app resolves it per instance
    (`MaestroTUI._predicate`) against its live-claim cache, which a module-level
    `(home, Snapshot)` predicate can't see."""
    del home, s
    return False


# Named filters: (display_name, row_predicate) — None predicate means no
# filtering (show all). A predicate takes (home, Snapshot) -> bool; wider than
# a bare phase set for callers that need one.
# T-161: sortable board columns, in table order; numeric ones first-sort descending.
_SORT_COLUMNS = ("Key", "Phase", "Title", "PR", "CI", "Fails", "Deps", "Idle")
_NUMERIC_SORT = frozenset({"Fails", "Deps", "PR", "Idle"})
_FILTERS: list[tuple[str, Callable[[Path, snap_mod.Snapshot], bool] | None]] = [
    ("needs-you", _needs_you_predicate),
    ("active", phase_predicate(ACTIVE_PHASES)),
    ("held", lambda home, s: fleet_mod.hold_state(home, s.key, store.now_epoch()) is not None),
    ("running", _running_placeholder),
    ("all", None),
]


# T-163: Now-cell thresholds, mirroring the watchdog's no-output rule
# (`dispatcher.run_watchdog`, `health.check_claim_no_output`).
_SILENT_WARN_S = 120
_SILENT_RED_FRACTION = 0.5
_DISPATCHER_OWNED = {"testrun": "tests", "restack": "restack"}


def _fmt_span(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s < 600 else f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _now_state(live: dict | None, now: float, no_output_timeout: int) -> tuple[str, str] | None:
    """(severity, text) for a key's Now cell, or None when blank. Severity is one of
    ok / warn / red / owned. Read-only: only displays what the watchdog would judge."""
    if not live or not live.get("claimed"):
        return None
    owned = _DISPATCHER_OWNED.get(live.get("kind") or "")
    if owned:
        return "owned", f"◌ {owned}"
    run = _fmt_span(now - live["epoch"]) if live.get("epoch") is not None else "?"
    mtime = live.get("mtime")
    if mtime is None:
        return "ok", f"● {run}"
    silent = now - mtime
    if no_output_timeout and silent > no_output_timeout * _SILENT_RED_FRACTION:
        return "red", f"silent {_fmt_span(silent)}/{_fmt_span(no_output_timeout)}"
    if silent > _SILENT_WARN_S:
        return "warn", f"● {run} silent {_fmt_span(silent)}"
    return "ok", f"● {run}"


def _load_live(home: Path, cfg: Config) -> dict[str, dict]:
    """Blocking (`ps` + stat): worker thread only. Never releases a claim."""
    raw = claims.all_claims(home)
    out: dict[str, dict] = {}
    for row in claims.describe_claims(home, max_age=cfg.unverified_claim_max_age):
        c = raw.get(row["key"], {})
        mtime = None
        if c.get("log_path"):
            try:
                mtime = os.stat(c["log_path"]).st_mtime
            except OSError:
                pass
        out[row["key"]] = {"claimed": row["claimed"], "kind": c.get("kind"),
                           "epoch": c.get("epoch"), "pid": c.get("pid"), "mtime": mtime}
    return out


def _load_board(home: Path) -> dict:
    """Everything `_populate` renders, read off the UI thread (T-163 enabler 4)."""
    try:
        cfg = config_mod.load(str(home))
    except store.MaestroError:
        cfg = Config(home=home)
    rows = ticket_rows(home)
    return {
        "rows": rows,
        "snaps": {r[-1]: snap_mod.load(home, r[-1]) for r in rows},
        "graph": depgraph.build(home),
        "live": _load_live(home, cfg),
        "no_output_timeout": cfg.no_output_timeout,
    }


# ANSWER_COMMANDS minus the ticket-level discard/retry (T-157).
_PER_QUESTION_COMMANDS = frozenset(ops_mod.ANSWER_COMMANDS) - {"discard", "retry"}


def _filter_idx_by_name(name: str) -> int:
    return next(i for i, (n, _) in enumerate(_FILTERS) if n == name)


class MaestroTUI(App):
    CSS = """
    Screen { layers: base topbar; }
    /* NB: do not put Header on a named layer — that stops its dock from
       reserving a flow row, which collapses #filter-bar underneath it. */
    #filter-bar {
        height: 1;
        background: $panel;
    }
    #pulse {
        height: 1;
    }
    #fleet-badge {
        layer: topbar;
        dock: top;
        height: 1;
        text-align: right;
        background: transparent;
    }
    #tickets { width: 2fr; height: 1fr; }
    #right   { width: 1fr; height: 1fr; }
    #detail  { width: 1fr; height: 1fr; padding: 0 1; }
    """

    # T-107: #tickets/#right fr split, bounding how far '[' / ']' can push it —
    # clamped so neither the list nor the detail column can vanish.
    _TICKETS_FR_MIN = 1.0
    _TICKETS_FR_MAX = 4.0
    _TICKETS_FR_STEP = 0.5

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "answer", "Answer"),
        Binding("c", "cmd", "Command"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("n", "create", "New"),
        Binding("enter", "focus_detail", "Detail"),
        Binding("i", "inbox_message", "Inbox"),
        Binding("[", "narrow_detail", "Detail-"),
        Binding("]", "widen_detail", "Detail+"),
        # Less-used actions: keys work but hidden from footer to reduce clutter
        Binding("ctrl+r", "retry", "Retry", show=False),
        Binding("ctrl+d", "discard", "Discard", show=False),
        Binding("F", "fleet_panel", "Fleet", show=False),
        Binding("T", "activity_panel", "Activity", show=False),
        Binding("D", "deps_panel", "Deps", show=False),
        Binding("e", "env_panel", "Env", show=False),
        Binding("S", "schedule_panel", "Schedule", show=False),
        Binding("s", "show_spec", "Spec", show=False),
        Binding("E", "edit_spec", "Edit spec", show=False),
        Binding("t", "toggle_tail", "Tail/Full", show=False),
        Binding("x", "compact", "Compact", show=False),
        Binding("z", "release", "Release", show=False),
        Binding("p", "project_rebuild", "Project", show=False),
        Binding("l", "view_logs", "Logs", show=False),
        Binding("I", "view_inbox", "Inbox log", show=False),
        Binding("o", "runner", "Runner", show=False),
        Binding("L", "import_linear", "Linear", show=False),
        Binding("A", "add_ac", "Add AC", show=False),
        Binding("v", "ac_matrix", "ACs", show=False),
        Binding("g", "suggest_acs", "Suggest ACs", show=False),
        Binding("Q", "trigger_post_qa", "Post-QA", show=False),
        Binding(">", "cycle_sort", "Sort column", show=False),
        Binding("<", "flip_sort", "Sort direction", show=False),
        Binding("question_mark", "show_help_panel", "Help", show=False),
        Binding("N", "toggle_nudge", "Nudge on/off", show=False),
        Binding("h", "hold", "Hold", show=False),
        Binding("j", "jump_running", "Next running", show=False),
    ]

    # Actions that act on one ticket: hidden on screens that aren't about a ticket.
    _TICKET_ACTIONS = frozenset({
        "answer", "cmd", "retry", "discard", "deps_panel", "show_spec", "edit_spec", "runner",
        "add_ac", "ac_matrix", "suggest_acs", "trigger_post_qa", "compact", "release", "hold", "focus_detail",
        "view_events", "inbox_message", "view_logs", "view_inbox",
    })
    # Board-only actions: meaningless once any other screen is pushed.
    _BOARD_ACTIONS = frozenset({
        "cycle_filter", "create", "narrow_detail", "widen_detail", "project_rebuild",
        "jump_running",
    })
    _NON_TICKET_SCREENS = (FleetScreen, EnvScreen, ScheduleScreen, ActivityScreen)
    _KEYED_SCREENS = (AcScreen, DetailScreen, SpecScreen, LogsScreen, EventsScreen, InboxScreen,
                      ProposalScreen)

    _selected_key: str | None = None
    _tail_mode: bool = True  # default: show tail in the sidebar panel
    _tickets_fr: float = 2.0  # #tickets fr share vs #right's fixed 1fr

    HELP = "Board: arrows move, enter opens a ticket, f cycles the filter, ? lists every key."

    def __init__(self, home: str, sessions_factory: Callable[[Config], object] | None = None) -> None:
        super().__init__()
        self._home = Path(home)
        # T-153: builds the SessionManager a post-input nudge sweep spawns through
        # (tests inject a DryRunSessions factory -- the only external boundary).
        self._sessions_factory = sessions_factory or build_routing_sessions
        # T-153: `N` toggles nudging for THIS session only (no config write).
        self._nudge_enabled: bool = True
        self._badge_result: dict | None = None
        self._selected_key: str | None = None
        self._filter_idx: int = 0
        # T-161: board sort -- index into _SORT_COLUMNS, None = default order.
        self._sort_col: int | None = None
        self._sort_desc: bool = False
        self._release_key: str | None = None
        self._tickets_fr: float = 2.0
        # key -> phase; None = first poll (no notifications)
        self._prev_phases: dict[str, str] | None = None
        # T-113: the ticket a pending "suggest-acs" worker is drafting for --
        # stashed here (not re-read from `self._selected_key` when the worker
        # resolves) so a table reselection while the ~120s claude call is in
        # flight can't attribute its result to the wrong ticket.
        self._suggest_acs_key: str | None = None
        # T-174: whether the last pulse tick saw "runaway" -- the toast fires
        # only on the False -> True edge.
        self._pulse_runaway: bool = False
        # key -> (phase, open-question count), refreshed by `_populate`; read by
        # `check_action`, which runs on every footer refresh and must not load snapshots.
        self._snap_cache: dict[str, tuple[str, int]] = {}
        # T-163: the worker-filled board cache `_populate` renders from.
        self._rows: list[tuple[str, ...]] = []
        self._snaps: dict[str, snap_mod.Snapshot] = {}
        self._graph = None
        self._live: dict[str, dict] = {}
        self._no_output_timeout: int = Config.no_output_timeout
        # key -> claim identity that already toasted red.
        self._red_toasted: dict[str, tuple] = {}

    def _target_key(self) -> str | None:
        """The ticket the operator is looking at: the pushed screen's own ticket, else the board cursor."""
        try:
            screen = self.screen
        except ScreenStackError:  # unmounted app (tests call actions directly)
            return self._selected_key
        if isinstance(screen, self._KEYED_SCREENS):
            return screen._key
        if isinstance(screen, DepsScreen):
            try:
                return screen._current_key()
            except NoMatches:
                return None
        return self._selected_key

    def _on_board(self) -> bool:
        return len(self.screen_stack) <= 1

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        try:
            screen = self.screen
        except ScreenStackError:
            return True
        if action in self._TICKET_ACTIONS and isinstance(screen, self._NON_TICKET_SCREENS):
            return False
        if action in self._BOARD_ACTIONS and not self._on_board():
            return False
        if action in ("retry", "discard"):
            cached = self._snap_cache.get(self._target_key() or "")
            if cached is not None and cached[0] != Phase.DEGRADED.value:
                return None
        elif action == "answer":
            cached = self._snap_cache.get(self._target_key() or "")
            if cached is not None and not cached[1]:
                return None
        return True

    def _refresh_bindings(self) -> None:
        try:
            self.screen.refresh_bindings()
        except ScreenStackError:
            pass

    def push_screen(self, *args, **kwargs):
        result = super().push_screen(*args, **kwargs)
        self.call_after_refresh(self._refresh_bindings)
        return result

    def pop_screen(self):
        result = super().pop_screen()
        self.call_after_refresh(self._refresh_bindings)
        return result

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="filter-bar")
        yield Static("", id="pulse")
        yield Static("", id="fleet-badge")
        with Horizontal():
            yield DataTable(id="tickets")
            with Vertical(id="right"):
                yield Static("[dim]Select a ticket[/dim]", id="detail")
                yield RichLog(id="events", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_column("Key")
        table.add_column("Phase")
        table.add_column("Title", width=40)
        table.add_column("PR")
        table.add_column("CI")
        table.add_column("Fails")
        table.add_column("Deps")
        table.add_column("Idle")
        table.add_column("Now")
        self._refresh_now()
        self.set_interval(3.0, self._kick_live)
        self._refresh_badge()
        self.set_interval(5.0, self._refresh_badge)
        self._refresh_pulse()
        self.set_interval(5.0, self._refresh_pulse)
        # T-153: reap exited reconcilers this long-lived process spawned, so a
        # zombie never keeps its claim `confirmed` and blocks the key's respawn.
        self.set_interval(3.0, reap_children)

    def _nudge(self, key: str | None) -> None:
        """T-153: right after a human inbox write, run a sweep on a thread worker
        -- key-scoped for *key*, UNFILTERED for ``None`` (create: ``key_filter``
        skips minting) -- and toast what it did. Never raises into the UI."""
        if not self._nudge_enabled:
            return
        try:
            cfg = config_mod.load(str(self._home))
        except store.MaestroError as e:
            self.notify(f"nudge skipped: {e}", severity="warning")
            return
        if not cfg.nudge_on_human_input:
            return
        factory = self._sessions_factory

        def _sweep() -> tuple[str, str]:
            try:
                report = disp.dispatch(cfg, factory(cfg), store.now_epoch(),
                                       key_filter=[key] if key else None)
            except store.MaestroError as e:
                return f"nudge: {e}", "warning"
            return _nudge_toast(key, report)

        self.run_worker(_sweep, thread=True, group=f"nudge-{key or '_create'}",
                        exclusive=True, name="nudge", exit_on_error=False)

    def action_toggle_nudge(self) -> None:
        self._nudge_enabled = not self._nudge_enabled
        self.notify(f"nudge {'on' if self._nudge_enabled else 'off'} for this session")
        if self._badge_result is not None:
            self._paint_badge()
        else:
            self._refresh_badge()

    def _paint_badge(self) -> None:
        r = self._badge_result
        try:  # the base screen, not whatever modal is on top right now
            self.screen_stack[0].query_one("#fleet-badge", Static).update(
                _render_badge(r["fleet"], r["provider"], self._nudge_enabled))
        except (NoMatches, IndexError):  # app is tearing down
            pass

    def _refresh_pulse(self) -> None:
        # T-174: reads only (no spend/ratelimit probe, no health.report()); the
        # spawn_budget snapshot scan is why this runs in a thread.
        def _load() -> dict:
            try:
                cfg = config_mod.load(str(self._home))
            except Exception:
                return {"error": True}
            now = store.now_epoch()
            rate = health.spawn_rate(self._home, now)
            budget = health.spawn_budget(cfg)
            hb = store.read_json(store.heartbeat_path(self._home), {})
            rl = ratelimit.status(self._home, now)
            return {
                "buckets": health.pulse_buckets(self._home, now),
                "spawns": rate["total"],
                "by_key": rate["by_key"],
                "budget": budget,
                "runaway": bool(budget) and rate["total"] > budget,
                "spend": spend_mod.status(cfg, now),
                "warn_fractions": list(cfg.alarm_spend_warn_fractions),
                "heartbeat": hb if isinstance(hb, dict) else {},
                "paused_until": rl.get("paused_until") if rl.get("paused") else None,
            }
        self.run_worker(_load, thread=True, group="pulse", exclusive=True, name="pulse")

    def _refresh_badge(self) -> None:
        # T-89 (AC1): the header badge is the one always-visible surface, so
        # this is where the provider_availability check's state rides along
        # with fleet.status -- FleetScreen's own health.report() call is far
        # too heavy to duplicate on a 5s timer, but check_provider_availability
        # alone is cheap (no probe unless the primary signal already tripped
        # or the board has no history at all, and even then rate-bounded by
        # cfg.provider_probe_interval_s -- see health._cached_probe).
        def _load() -> dict:
            cfg = Config(home=self._home)
            return {
                "fleet": fleet_mod.status(self._home),
                "provider": health.check_provider_availability(cfg, store.now_epoch()),
            }
        self.run_worker(_load, thread=True, group="badge", exclusive=True, name="fleet-badge")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "fleet-badge" and event.state == WorkerState.SUCCESS:
            self._badge_result = event.worker.result
            self._paint_badge()
        elif event.worker.name == "nudge":
            if event.state == WorkerState.SUCCESS:
                msg, severity = event.worker.result
                self.notify(msg, severity=severity)
            elif event.state == WorkerState.ERROR:
                self.notify(f"nudge failed: {event.worker.error}", severity="error")
        elif event.worker.name == "pulse" and event.state == WorkerState.SUCCESS:
            p = event.worker.result
            try:
                self.screen_stack[0].query_one("#pulse", Static).update(_render_pulse(p))
            except NoMatches:  # app is tearing down
                return
            runaway = bool(p.get("runaway"))
            if runaway and not self._pulse_runaway:
                by_key = p.get("by_key") or {}
                top = max(by_key, key=by_key.get) if by_key else "?"
                self.notify(
                    f"Runaway spawn rate: {p['spawns']}/{p['budget']} per hour, "
                    f"top key {top} -- press F for the fleet panel",
                    severity="error")
            self._pulse_runaway = runaway
        elif event.worker.name == "pulse" and event.state == WorkerState.ERROR:
            try:
                self.screen_stack[0].query_one("#pulse", Static).update("[red]pulse: error[/red]")
            except NoMatches:
                pass
        elif event.worker.name == "release-probe":
            if event.state == WorkerState.SUCCESS:
                self._on_release_probed(self._release_key, event.worker.result)
            elif event.state == WorkerState.ERROR:
                self.notify(f"Claim probe failed: {event.worker.error}", severity="error")
        elif event.worker.name == "compact":
            if event.state == WorkerState.SUCCESS:
                r = event.worker.result
                self.notify(
                    f"Compacted: {r.get('archived', 0)} archived, {r.get('remaining', 0)} remaining"
                )
            elif event.state == WorkerState.ERROR:
                self.notify(f"Compact failed: {event.worker.error}", severity="error")
        elif event.worker.name == "project-rebuild":
            if event.state == WorkerState.SUCCESS:
                self.notify(str(event.worker.result))
            elif event.state == WorkerState.ERROR:
                self.notify(f"Project failed: {event.worker.error}", severity="error")
        elif event.worker.name == "suggest-acs":
            key = self._suggest_acs_key
            if event.state == WorkerState.SUCCESS:
                if key is not None:
                    self._open_suggest_acs_modal(key, event.worker.result)
            elif event.state == WorkerState.ERROR:
                # T-113 AC3: a failed/unavailable claude invocation surfaces as an
                # error notify -- spec.md is untouched (nothing was ever written
                # here) and the app never crashes (this branch is exactly what
                # keeps `event.worker.error` from propagating further).
                self.notify(f"Suggest ACs failed: {event.worker.error}", severity="error")
        elif event.worker.name == "trigger-post-qa":
            if event.state == WorkerState.SUCCESS:
                r = event.worker.result
                self.notify(f"post_qa_skill fired for {r['key']} (runner={r['runner']})")
            elif event.state == WorkerState.ERROR:
                # T-117: a MaestroError (no post_qa_skill configured, an active
                # session, a failed runner preflight) surfaces as an error
                # notify -- the app must never crash on it, same posture as
                # "suggest-acs" above.
                self.notify(f"Trigger post-QA failed: {event.worker.error}", severity="error")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None
        self._selected_key = key
        self._refresh_bindings()
        detail = self.query_one("#detail", Static)
        if key is None:
            detail.update("[dim]Select a ticket[/dim]")
            self.query_one("#events", RichLog).clear()
            return
        # cursor moves (incl. the one a refresh makes) render from the worker's cache
        self._show_detail(key, self._snaps.get(key))

    def _show_detail(self, key: str, snap: snap_mod.Snapshot | None = None) -> None:
        detail = self.query_one("#detail", Static)
        if snap is None:
            snap = snap_mod.load(self._home, key)
        runner, runner_model = spec_runner(self._home, key)
        detail.update(_render_detail(snap, snap_mod.display_title(self._home, snap),
                                     runner, runner_model,
                                     hold=fleet_mod.hold_state(self._home, key, store.now_epoch())))
        self._refresh_events()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a focused DataTable emits RowSelected and consumes the key, so
        # the app-level `enter` binding never fires — open the detail view here.
        key = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None
        if key is not None:
            self._selected_key = key
            self.push_screen(DetailScreen(self._home, key))

    def action_refresh(self) -> None:
        self._kick_live()

    # --- T-163: live board cache -------------------------------------------

    def _kick_live(self) -> None:
        """Reload the board cache on an exclusive thread worker, then re-render once."""
        home = self._home

        def _work() -> None:
            data = _load_board(home)
            try:
                self.call_from_thread(self._apply_board, data)
            except RuntimeError:  # app torn down mid-load
                pass

        self.run_worker(_work, thread=True, group="live", exclusive=True,
                        name="live", exit_on_error=False)

    def _refresh_now(self) -> None:
        """Synchronous reload + render (mount and tests; never the periodic path)."""
        self._apply_board(_load_board(self._home))

    def _apply_board(self, data: dict) -> None:
        self._rows = data["rows"]
        self._snaps = data["snaps"]
        self._graph = data["graph"]
        self._live = data["live"]
        self._no_output_timeout = data["no_output_timeout"]
        self._toast_new_red(store.now_epoch())
        self._populate()

    def _toast_new_red(self, now: float) -> None:
        red: dict[str, tuple] = {}
        for key, live in self._live.items():
            st = _now_state(live, now, self._no_output_timeout)
            if st and st[0] == "red":
                red[key] = (live.get("pid"), live.get("epoch"))
        for key, ident in red.items():
            if self._red_toasted.get(key) != ident:
                self.notify(f"{key}: session silent past 50% of no_output_timeout",
                            severity="warning", timeout=8)
        self._red_toasted = red

    def _running_keys(self) -> set[str]:
        return {k for k, v in self._live.items() if v.get("claimed")}

    def _predicate(self, pred):
        if pred is _running_placeholder:
            running = self._running_keys()
            return lambda home, s: s.key in running
        return pred

    def _now_cell(self, key: str, now: float) -> Text | str:
        st = _now_state(self._live.get(key), now, self._no_output_timeout)
        if st is None:
            return ""
        sev, text = st
        style = {"ok": "green", "warn": "yellow", "red": "bold red", "owned": "dim"}[sev]
        return Text(text, style=style)

    def action_jump_running(self) -> None:
        """Select the next running row after the cursor, wrapping; widens the filter
        to `all` only when the target isn't visible."""
        running = self._running_keys()
        order = [r[-1] for r in self._rows if r[-1] in running]
        if not order:
            self.notify("No running tickets")
            return
        table = self.query_one(DataTable)
        visible = [str(k.value) for k in table.rows]
        cur = self._selected_key
        pos = visible.index(cur) if cur in visible else -1
        after = [k for k in visible[pos + 1:] if k in running]
        target = after[0] if after else next((k for k in visible if k in running), None)
        if target is None:
            target = order[0]
        if target not in visible:
            self._filter_idx = _filter_idx_by_name("all")
            self._populate()
            visible = [str(k.value) for k in table.rows]
        if target in visible:
            table.move_cursor(row=visible.index(target))
            self._selected_key = target
            self._show_detail(target)

    def action_cycle_filter(self) -> None:
        self._filter_idx = (self._filter_idx + 1) % len(_FILTERS)
        self._populate()

    def _set_sort(self, col: int | None) -> None:
        self._sort_col = col
        if col is not None:
            self._sort_desc = _SORT_COLUMNS[col] in _NUMERIC_SORT
        self._populate()

    def action_cycle_sort(self) -> None:
        nxt = 0 if self._sort_col is None else self._sort_col + 1
        self._set_sort(nxt if nxt < len(_SORT_COLUMNS) else None)

    def action_flip_sort(self) -> None:
        if self._sort_col is None:
            return
        self._sort_desc = not self._sort_desc
        self._populate()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col = event.column_index
        if col >= len(_SORT_COLUMNS):
            return
        if col == self._sort_col:
            self.action_flip_sort()
        else:
            self._set_sort(col)

    def _sort_visible(self, visible: list, snaps_by_key: dict, graph) -> list:
        """Stable sort of *visible* rows by the active column, on typed values."""
        if self._sort_col is None:
            return visible
        name = _SORT_COLUMNS[self._sort_col]
        now = store.iso_to_epoch(store.iso_now()) or 0.0

        def keyfn(row):
            key = row[-1]
            snap = snaps_by_key[key]
            if name == "Key":
                return disp.split_key(key)
            if name == "Phase":
                return _PHASE_RANK.get(snap.phase, 99)
            if name == "Title":
                return row[2].lower()
            if name == "PR":
                return snap.pr_number or 0
            if name == "CI":
                return row[4].lower()
            if name == "Fails":
                return snap.failure_count
            if name == "Deps":
                node = graph.nodes.get(key)
                return len(node.open_deps) + len(node.missing) if node and node.blocked else 0
            return _idle_seconds(snap, now) or 0.0  # Idle

        return sorted(visible, key=keyfn, reverse=self._sort_desc)

    def action_cmd(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)

        def _on_dismiss(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            command, args_text = result
            args = {"text": args_text} if args_text else {}
            # No qid is carried, so fold_inbox would answer EVERY open question.
            n_open = len(snap_mod.load(self._home, key).open_questions)
            if command in _PER_QUESTION_COMMANDS and n_open > 1:
                self.notify(
                    f"{key} has {n_open} open questions — '{command}' would answer all of "
                    "them; use `a` to answer one at a time",
                    severity="warning",
                )
                return

            def _queue() -> None:
                inbox.append_command(self._home, key, command, args)
                self.notify(f"'{command}' queued for {key}")
                self._nudge(key)

            if command == "discard":
                self._confirm_discard(key, _queue)
            else:
                _queue()

        self.push_screen(_CmdModal(key, snap.phase), _on_dismiss)

    def action_retry(self) -> None:
        self._send_degraded_cmd("retry")

    def action_discard(self) -> None:
        self._send_degraded_cmd("discard")

    def _send_degraded_cmd(self, command: str) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)
        if snap.phase != Phase.DEGRADED.value:
            self.notify(f"{key}: '{command}' only applies to degraded tickets", severity="warning")
            return

        def _queue() -> None:
            inbox.append_command(self._home, key, command, {})
            self.notify(f"'{command}' queued for {key}")
            self._nudge(key)

        if command == "discard":
            self._confirm_discard(key, _queue)
        else:
            _queue()

    def _confirm_discard(self, key: str, queue: Callable[[], None]) -> None:
        """Discard is irreversible: require the ticket key typed out before queuing."""
        def _on_confirm(ok: bool | None) -> None:
            if ok:
                queue()

        self.push_screen(
            _ConfirmModal(f"Discard [bold]{key}[/bold]? This cannot be undone.", require=key),
            _on_confirm,
        )

    def action_fleet_panel(self) -> None:
        self.push_screen(FleetScreen(self._home))

    def action_activity_panel(self) -> None:
        self.push_screen(ActivityScreen(self._home))

    def action_deps_panel(self) -> None:
        self.push_screen(DepsScreen(self._home, self._target_key()))

    def action_show_spec(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(SpecScreen(self._home, key))

    def action_edit_spec(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        warning = edit_in_editor(self, self._home / "tickets" / key / "spec.md")
        if warning:
            self.notify(warning, severity="warning")
            return
        if key == self._selected_key and self._on_board():
            self._show_detail(key)

    def action_runner(self) -> None:
        """UX-2: open the runner modal for the selected ticket. All state
        mutation goes through UX-1's `ops.set_runner` in `_on_dismiss` -- the
        TUI never hand-edits the spec file. The `key is None` guard is required
        by the binding sweep, which presses every key with no ticket selected."""
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        runner, runner_model = spec_runner(self._home, key)

        def _on_dismiss(result: dict | None) -> None:
            if result is None:
                return
            cfg = Config(home=self._home)
            try:
                outcome = ops_mod.set_runner(cfg, key, runner=result["runner"],
                                             runner_model=result["runner_model"])
            except store.MaestroError as e:
                self.notify(str(e), severity="warning")
                return
            if outcome.get("warning"):
                self.notify(outcome["warning"], severity="warning")
            else:
                self.notify(f"runner updated for {key}")

        self.push_screen(_RunnerModal(key, runner, runner_model, home=self._home), _on_dismiss)

    def action_add_ac(self) -> None:
        """T-112: open the add-AC modal for the selected ticket. All state
        mutation goes through `ops.add_ac` in `_on_dismiss` -- the TUI never
        hand-edits the spec file. The `key is None` guard is required by the
        binding sweep, which presses every key with no ticket selected."""
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return

        def _on_dismiss(text: str | None) -> None:
            if text is None:
                return
            cfg = Config(home=self._home)
            try:
                ops_mod.add_ac(cfg, key, text)
            except store.MaestroError as e:
                self.notify(str(e), severity="warning")
                return
            self.notify(f"AC added to {key}")

        self.push_screen(_AddAcModal(key), _on_dismiss)

    def action_suggest_acs(self) -> None:
        """T-113: draft suggested ACs for the selected AC-less ticket via a
        bounded `claude -p` capture call (`ops.suggest_acs`) -- run on a
        worker thread (`run_worker(thread=True)`, same shape as
        `action_compact`) so the TUI never blocks on it. Refuses up front
        with a warning notify, writing nothing, if the ticket already has
        ACs (`snapshot.has_acs` gate, per the spec) -- the `key is None`
        guard is required by the binding sweep, which presses every key with
        no ticket selected. The review modal only opens once the worker
        resolves (`on_worker_state_changed`'s "suggest-acs" branch); a
        failed/unavailable spawn there surfaces as an error notify instead --
        `exit_on_error=False` is required for that: `run_worker`'s default
        (`True`) would otherwise hand a raised `MaestroError` to the App's
        own exception handler too (AC3 says a failed invocation must not
        crash the app -- this is the actual guard for that, not just the
        `on_worker_state_changed` notify below)."""
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        spec_file = store.spec_path(self._home, key)
        spec_text = spec_file.read_text(encoding="utf-8") if spec_file.exists() else ""
        if snap_mod.has_acs(spec_text):
            self.notify(f"{key} already has acceptance criteria", severity="warning")
            return
        cfg = Config(home=self._home)
        self._suggest_acs_key = key
        self.run_worker(lambda: ops_mod.suggest_acs(cfg, key), thread=True, name="suggest-acs",
                        exit_on_error=False)

    def _open_suggest_acs_modal(self, key: str, suggestions: list[str]) -> None:
        """Push the review modal for a resolved "suggest-acs" worker; on
        accept, writes exactly the accepted suggestions via `ops.add_ac`
        (T-112's verb, one call per accepted string -- it has no batch form
        of its own); cancelling (or accepting zero checked boxes) writes
        nothing."""
        def _on_dismiss(accepted: list[str] | None) -> None:
            if not accepted:
                return
            cfg = Config(home=self._home)
            for text in accepted:
                try:
                    ops_mod.add_ac(cfg, key, text)
                except store.MaestroError as e:
                    self.notify(str(e), severity="warning")
                    return
            self.notify(f"{len(accepted)} AC(s) added to {key}")

        self.push_screen(_SuggestAcsModal(key, suggestions), _on_dismiss)

    def action_trigger_post_qa(self) -> None:
        """T-117: manual escape hatch for `post_qa_skill`
        (`dispatcher.trigger_post_qa_skill`) -- run on a worker thread
        (`run_worker(thread=True)`, same shape as `action_suggest_acs`) so the
        TUI never blocks on the spawn; `exit_on_error=False` for the same
        reason as `action_suggest_acs` -- a raised `MaestroError` (no
        `post_qa_skill` configured, an active session, a failed runner
        preflight) must surface via `on_worker_state_changed`'s notify, not
        the App's own exception handler. The `key is None` guard is required
        by the binding sweep, which presses every key with no ticket
        selected. Same `sessions.build_routing_sessions` factory as `cli.cmd_dispatch`/
        `cli._nudge` -- every registered non-claude backend wired, so a
        manual fire can route to whatever `post_qa_skill_runner` names."""
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        cfg = config_mod.load(str(self._home))
        sessions = self._sessions_factory(cfg)
        self.run_worker(lambda: disp.trigger_post_qa_skill(cfg, sessions, key),
                        thread=True, name="trigger-post-qa", exit_on_error=False)

    def action_env_panel(self) -> None:
        self.push_screen(EnvScreen(self._home))

    def action_schedule_panel(self) -> None:
        self.push_screen(ScheduleScreen(self._home))

    def action_create(self) -> None:
        prefixes = existing_prefixes(self._home)

        def _on_dismiss(result: dict | None) -> None:
            if result is None:
                return
            create_args: dict = {
                "priority": result["priority"],
            }
            if result.get("intent"):
                create_args["intent"] = result["intent"]
            if result.get("kind"):
                create_args["kind"] = result["kind"]
            if result.get("model"):
                create_args["model"] = result["model"]
            if result.get("effort"):
                create_args["effort"] = result["effort"]
            inbox.append_new(
                self._home,
                result["title"],
                key=None,
                args=create_args,
                prefix=result.get("prefix"),
            )
            self.notify("queued; dispatcher will mint the key")
            self._nudge(None)

        self.push_screen(_CreateModal(prefixes), _on_dismiss)

    def action_import_linear(self) -> None:
        """T-103: paste a Linear issue URL/identifier, mint the ticket
        synchronously through `ops.import_linear` (the same entrypoint
        `maestro import-linear` calls), and refresh the table so the new
        ticket shows up immediately instead of waiting for the next poll."""
        def _on_dismiss(url_or_id: str | None) -> None:
            if url_or_id is None:
                return
            cfg = config_mod.load(str(self._home))
            try:
                result = ops_mod.import_linear(cfg, url_or_id)
            except store.MaestroError as e:
                self.notify(str(e), severity="error")
                return
            if result["minted"]:
                self.notify(f"imported {result['key']}")
                self._kick_live()
            else:
                self.notify(f"{result['key']} already imported", severity="warning")

        self.push_screen(_ImportLinearModal(), _on_dismiss)

    def action_answer(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)
        if not snap.open_questions:
            self.notify(f"No open questions for {key}", severity="warning")
            return
        # `open_questions` round-trips through a sort_keys=True JSON snapshot, so
        # the dict comes back qid-alphabetical, not round order -- walk it in the
        # round's own 1..N order (via the text's own "N/total." prefix) instead,
        # else the "N of M" position shown per-question would visibly scramble.
        # Plain (non-round) questions carry no position; a stable sort leaves
        # those in their existing (alphabetical) relative order, at the end.
        questions = sorted(
            snap.open_questions.items(),
            key=lambda qt: ops_mod.parse_round_question(qt[1])[0] or float("inf"),
        )
        self._walk_questions(key, questions, 0, 0)

    def _walk_questions(
        self, key: str, questions: list[tuple[str, str]], idx: int, answered: int
    ) -> None:
        if idx >= len(questions):
            if answered:
                self.notify(f"{answered} answer(s) queued for {key}")
                if key == self._selected_key and self._on_board():
                    self._show_detail(key)
                self._nudge(key)  # once, however the walk ended
            return
        qid, text = questions[idx]
        remaining = len(questions) - idx
        position, total, body, recommend = ops_mod.parse_round_question(text)

        def _on_dismiss(answer: object) -> None:
            if answer is None:
                # Esc partway: nudge for whatever was already queued.
                self._walk_questions(key, [], 0, answered)
                return
            if answer is _ACCEPT_ALL:
                # Queue the recommendation for every remaining question that has
                # one; keep walking (via modal, one at a time) only the ones that
                # don't -- fast-tracks the recommended ones without silently
                # skipping the ones that still need a typed answer. T-140: each
                # queued command carries the accept marker, same as a lone
                # Ctrl+R (below) -- Ctrl+G is just "Ctrl+R for every remaining
                # recommended question in the round".
                queued = 0
                unanswered: list[tuple[str, str]] = []
                for q_qid, q_text in questions[idx:]:
                    _, _, _, q_recommend = ops_mod.parse_round_question(q_text)
                    if q_recommend:
                        inbox.append_command(
                            self._home, key, "ans",
                            {"qid": q_qid, "text": q_recommend, "accepted_recommendation": True})
                        queued += 1
                    else:
                        unanswered.append((q_qid, q_text))
                if queued:
                    self.notify(f"{queued} recommendation(s) queued for {key}")
                self._walk_questions(key, unanswered, 0, answered + queued)
                return
            # T-140: Ctrl+R dismisses with an `_AcceptedRecommendation` (a str
            # subclass equal to the recommendation) -- carry the accept marker
            # onto the queued command only then, never for an identical TYPED
            # answer (a plain `str`, no marker).
            args = {"qid": qid, "text": str(answer)}
            if isinstance(answer, _AcceptedRecommendation):
                args["accepted_recommendation"] = True
            inbox.append_command(self._home, key, "ans", args)
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(
            _AnswerModal(key, qid, position, total, body, recommend, remaining, self._home),
            _on_dismiss,
        )

    def action_compact(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return

        def _on_confirm(ok: bool) -> None:
            if not ok:
                return
            cfg = Config(home=self._home)
            self.run_worker(lambda: ops_mod.compact(cfg, key), thread=True, name="compact")

        self.app.push_screen(
            _ConfirmModal(f"Compact log for [bold]{key}[/bold]?"), _on_confirm
        )

    def action_hold(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        if fleet_mod.hold_state(self._home, key, store.now_epoch()) is not None:
            def _on_confirm(ok: bool | None) -> None:
                if not ok:
                    return
                fleet_mod.unhold(self._home, key)
                self.notify(f"Released hold on {key}")
                self._populate()

            self.push_screen(
                _ConfirmModal(f"{key} is held. Release the hold?"), _on_confirm)
            return

        def _on_dismiss(result: tuple[int | None, str | None] | None) -> None:
            if result is None:
                return
            seconds, reason = result
            until = store.now_epoch() + seconds if seconds is not None else None
            fleet_mod.hold(self._home, key, until=until, reason=reason)
            self.notify(f"Held {key}")
            self._populate()

        self.push_screen(HoldModal(key), _on_dismiss)

    def action_release(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        cfg = Config(home=self._home)
        # describe_claims shells out to `ps`, so probe off the UI thread. Never
        # active_keys/is_claimed: both release stale claims as a side effect.
        self._release_key = key
        self.run_worker(
            lambda: claims.describe_claims(self._home, max_age=cfg.unverified_claim_max_age),
            thread=True, name="release-probe",
        )

    def _on_release_probed(self, key: str, rows: list[dict]) -> None:
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            self.notify(f"{key}: no claim to release")
            return
        if row["claimed"]:
            self.notify(
                f"{key}: claim is live (pid {row['pid']}, {row['verdict']})", severity="warning")
            return

        def _on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            claims.release(self._home, key)
            self.notify(f"Claim released for {key}")

        self.push_screen(
            _ConfirmModal(f"Release claim for [bold]{key}[/bold]?"), _on_confirm
        )

    def action_project_rebuild(self) -> None:
        self.notify("Rebuilding projection…")
        self.run_worker(self._run_project, thread=True, name="project-rebuild")

    def _run_project(self) -> str:
        try:
            from .. import projection
            written = projection.write(self._home)
            return f"Wrote {len(written)} projection files"
        except Exception as exc:
            return str(exc)

    def action_toggle_tail(self) -> None:
        self._tail_mode = not self._tail_mode
        self._refresh_events()

    def action_narrow_detail(self) -> None:
        """Shrink the right-hand detail column (grow #tickets), clamped."""
        self._tickets_fr = min(self._TICKETS_FR_MAX, self._tickets_fr + self._TICKETS_FR_STEP)
        self._apply_split()

    def action_widen_detail(self) -> None:
        """Grow the right-hand detail column (shrink #tickets), clamped."""
        self._tickets_fr = max(self._TICKETS_FR_MIN, self._tickets_fr - self._TICKETS_FR_STEP)
        self._apply_split()

    def _apply_split(self) -> None:
        # #right (the Vertical wrapping #detail + #events) stays a fixed 1fr in
        # CSS, so varying #tickets' fr alone moves the whole list/detail split.
        self.query_one("#tickets", DataTable).styles.width = f"{self._tickets_fr}fr"

    def action_focus_detail(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(DetailScreen(self._home, key))

    def action_ac_matrix(self) -> None:
        """T-167: the AC evidence matrix for the ticket being looked at."""
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        if isinstance(self.screen, AcScreen):
            return
        self.push_screen(AcScreen(self._home, key))

    def action_view_events(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(EventsScreen(self._home, key))

    def action_inbox_message(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return

        def _on_dismiss(text: str | None) -> None:
            if text is None:
                return
            inbox.append_command(self._home, key, "msg", {"text": text})
            self.notify(f"Message queued for {key}")
            self._nudge(key)

        self.push_screen(_InboxModal(key), _on_dismiss)

    def action_view_logs(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(LogsScreen(self._home, key))

    def action_view_inbox(self) -> None:
        key = self._target_key()
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(InboxScreen(self._home, key))

    def _populate(self) -> None:
        """Render the filter bar, rows and Now cells from the cached board (no I/O)."""
        _name, predicate = _FILTERS[self._filter_idx]
        predicate = self._predicate(predicate)
        home = self._home
        all_rows = self._rows
        snaps_by_key = self._snaps

        # Detect tickets newly entering awaiting-human/degraded.
        new_phases = {key: s.phase for key, s in snaps_by_key.items()}
        if self._prev_phases is not None:
            for key, phase in new_phases.items():
                prev_phase = self._prev_phases.get(key)
                if phase in _NEEDS_YOU_PHASE_VALUES and prev_phase != phase:
                    self.notify(f"{key}: {phase}", severity="warning", timeout=6)
        self._prev_phases = new_phases
        self._snap_cache = {k: (sn.phase, len(sn.open_questions)) for k, sn in snaps_by_key.items()}

        # Build filter bar: show counts per filter, bold the active one
        parts = []
        for i, (fname, fpred) in enumerate(_FILTERS):
            if fpred is None:
                count = len(all_rows)
            else:
                fpred = self._predicate(fpred)
                count = sum(1 for s in snaps_by_key.values() if fpred(home, s))
            label = f"{fname}({count})"
            if i == self._filter_idx:
                label = f"[reverse bold] {label} [/reverse bold]"
            else:
                label = f"[dim]{label}[/dim]"
            parts.append(label)
        bar = "  " + "  |  ".join(parts)
        if self._sort_col is not None:
            bar += f"  {'↓' if self._sort_desc else '↑'} {_SORT_COLUMNS[self._sort_col]}"
        self.query_one("#filter-bar", Static).update(bar)

        # Apply current filter
        if predicate is not None:
            visible = [r for r in all_rows if predicate(home, snaps_by_key[r[-1]])]
        else:
            visible = all_rows

        graph = self._graph  # worker-cached; no I/O on the UI thread
        visible = self._sort_visible(visible, snaps_by_key, graph)

        table = self.query_one(DataTable)
        # Preserve cursor across clear/repopulate.
        prev_key: str | None = None
        try:
            rk = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            if rk is not None and rk.value is not None:
                prev_key = str(rk.value)
        except Exception:
            pass
        prev_row = table.cursor_row
        table.clear()
        row_keys: list[str] = []
        palette = self.get_css_variables()
        now = store.now_epoch()
        for *cells, row_key in visible:
            if fleet_mod.hold_state(home, row_key, store.now_epoch()) is not None:
                cells[0] = f"⏸ {cells[0]}"
            styled = _styled_row(*cells)
            idle = _format_age(_idle_seconds(snaps_by_key[row_key], now))
            table.add_row(*styled, self._deps_cell(graph, row_key, palette), idle,
                          self._now_cell(row_key, now), key=row_key)
            row_keys.append(row_key)
        if not row_keys:
            self._selected_key = None
            self.query_one("#detail", Static).update("[dim]No tickets match[/dim]")
            self.query_one("#events", RichLog).clear()
            self._refresh_bindings()
            return
        if prev_key and prev_key in row_keys:
            table.move_cursor(row=row_keys.index(prev_key))
        else:
            table.move_cursor(row=min(prev_row, len(row_keys) - 1))
        if self._selected_key:
            self._refresh_events()
        self._refresh_bindings()

    @staticmethod
    def _deps_cell(graph, key: str, palette: dict) -> Text | str:
        """Open-dependency count colored by blocking depth; blank when unblocked."""
        node = graph.nodes.get(key) if graph is not None else None
        if node is None or not node.blocked:
            return ""
        count = len(node.open_deps) + len(node.missing)
        return Text(str(count), style=_dep_color(graph.depth[key], palette))

    def _refresh_events(self) -> None:
        if not self._selected_key:
            return
        events = event_log.read(self._home, self._selected_key)
        log = self.query_one("#events", RichLog)
        log.clear()
        for line in render_log(events, tail=self._tail_mode):
            log.write(line)


def _idle_seconds(snap, now: float) -> float | None:
    """Seconds since the ticket's last event (`updated_ts`); None when unknown."""
    ts = store.iso_to_epoch(snap.updated_ts) if snap.updated_ts else None
    return None if ts is None else max(0.0, now - ts)


def _format_age(seconds: float | None) -> str:
    """Compact age: 42s, 7m, 3h, 2d; blank when unknown."""
    if seconds is None:
        return ""
    s = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def main(args) -> int:
    from ..config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
