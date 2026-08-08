"""The MaestroTUI app: main board table, key actions, and the `maestro tui` entrypoint."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static
from textual.worker import Worker, WorkerState

from .. import claims, event_log, fleet as fleet_mod, inbox, ops as ops_mod, snapshot as snap_mod
from ..config import Config
from ..dispatcher import existing_prefixes, needs_approval, spec_tier
from ..projection import phase_predicate, ticket_rows
from ..statemachine import Phase, ACTIVE_PHASES
from .detail import render as _render_detail
from .events import render_log
from .modals import _ACCEPT_ALL, _AnswerModal, _CmdModal, _ConfirmModal, _CreateModal, _InboxModal
from .render import _render_badge, _styled_row
from .screens import (
    DetailScreen,
    EnvScreen,
    EventsScreen,
    FleetScreen,
    LogsScreen,
    ScheduleScreen,
    SpecScreen,
)

_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})
_NEEDS_YOU_PHASE_VALUES = {p.value for p in _NEEDS_YOU_PHASES}


def _needs_you_predicate(home: Path, s: snap_mod.Snapshot) -> bool:
    """The needs-you filter: the two sleeping-and-stuck phases, plus the
    tier-2 approval gate (GA-21) -- a ticket that's still "implementing" but
    not due until `maestro approve`, so it can't be expressed as a phase."""
    return s.phase in _NEEDS_YOU_PHASE_VALUES or needs_approval(home, s.key, s)


# Named filters: (display_name, row_predicate) — None predicate means no
# filtering (show all). A predicate takes (home, Snapshot) -> bool; wider than
# a bare phase set since needs-you (above) can't be expressed as one.
_FILTERS: list[tuple[str, Callable[[Path, snap_mod.Snapshot], bool] | None]] = [
    ("needs-you", _needs_you_predicate),
    ("active", phase_predicate(ACTIVE_PHASES)),
    ("all", None),
]


class MaestroTUI(App):
    CSS = """
    Screen { layers: base topbar; }
    /* NB: do not put Header on a named layer — that stops its dock from
       reserving a flow row, which collapses #filter-bar underneath it. */
    #filter-bar {
        height: 1;
        background: $panel;
    }
    #fleet-badge {
        layer: topbar;
        dock: top;
        height: 1;
        text-align: right;
        background: transparent;
    }
    #tickets { width: 2fr; height: 1fr; }
    #detail  { width: 1fr; height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "answer", "Answer"),
        Binding("c", "cmd", "Command"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("n", "create", "New"),
        Binding("enter", "focus_detail", "Detail"),
        Binding("i", "inbox_message", "Inbox"),
        # Less-used actions: keys work but hidden from footer to reduce clutter
        Binding("ctrl+r", "retry", "Retry", show=False),
        Binding("ctrl+d", "discard", "Discard", show=False),
        Binding("F", "fleet_panel", "Fleet", show=False),
        Binding("e", "env_panel", "Env", show=False),
        Binding("S", "schedule_panel", "Schedule", show=False),
        Binding("s", "show_spec", "Spec", show=False),
        Binding("t", "toggle_tail", "Tail/Full", show=False),
        Binding("x", "compact", "Compact", show=False),
        Binding("z", "release", "Release", show=False),
        Binding("p", "project_rebuild", "Project", show=False),
        Binding("l", "view_logs", "Logs", show=False),
    ]

    _selected_key: str | None = None
    _tail_mode: bool = True  # default: show tail in the sidebar panel

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)
        self._selected_key: str | None = None
        self._filter_idx: int = 0
        # key -> (phase, gated); None = first poll (no notifications)
        self._prev_phases: dict[str, tuple[str, bool]] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="filter-bar")
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
        table.add_column("Tier")
        table.add_column("Fails")
        self._populate()
        self.set_interval(3.0, self._populate)
        self._refresh_badge()
        self.set_interval(5.0, self._refresh_badge)

    def _refresh_badge(self) -> None:
        self.run_worker(lambda: fleet_mod.status(self._home), thread=True,
                        group="badge", exclusive=True, name="fleet-badge")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "fleet-badge" and event.state == WorkerState.SUCCESS:
            self.query_one("#fleet-badge", Static).update(_render_badge(event.worker.result))
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None
        self._selected_key = key
        detail = self.query_one("#detail", Static)
        if key is None:
            detail.update("[dim]Select a ticket[/dim]")
            self.query_one("#events", RichLog).clear()
            return
        snap = snap_mod.load(self._home, key)
        detail.update(_render_detail(snap, spec_tier(self._home, key)))
        self._refresh_events()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a focused DataTable emits RowSelected and consumes the key, so
        # the app-level `enter` binding never fires — open the detail view here.
        key = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None
        if key is not None:
            self._selected_key = key
            self.push_screen(DetailScreen(self._home, key))

    def action_refresh(self) -> None:
        self._populate()

    def action_cycle_filter(self) -> None:
        self._filter_idx = (self._filter_idx + 1) % len(_FILTERS)
        self._populate()

    def action_cmd(self) -> None:
        key = self._selected_key
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)
        gated = needs_approval(self._home, key, snap)

        def _on_dismiss(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            command, args_text = result
            if command == "approve" and gated:
                # The tier-2 gate clears via its own dedicated event (`ops.approve`
                # / `maestro approve`), not the inbox-command path below -- a
                # queued "approve" command would just no-op here (this ticket has
                # no open_questions for ANSWER_COMMANDS to attach an answer to).
                cfg = Config(home=self._home)
                ops_mod.approve(cfg, key)
                self.notify(f"approved {key}")
                return
            args = {"text": args_text} if args_text else {}
            inbox.append_command(self._home, key, command, args)
            self.notify(f"'{command}' queued for {key}")

        self.push_screen(_CmdModal(key, snap.phase, gated=gated), _on_dismiss)

    def action_retry(self) -> None:
        self._send_degraded_cmd("retry")

    def action_discard(self) -> None:
        self._send_degraded_cmd("discard")

    def _send_degraded_cmd(self, command: str) -> None:
        key = self._selected_key
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)
        if snap.phase != Phase.DEGRADED.value:
            self.notify(f"'{command}' only applies to degraded tickets", severity="warning")
            return
        inbox.append_command(self._home, key, command, {})
        self.notify(f"'{command}' queued for {key}")

    def action_fleet_panel(self) -> None:
        self.push_screen(FleetScreen(self._home))

    def action_show_spec(self) -> None:
        if self._selected_key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(SpecScreen(self._home, self._selected_key))

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
                "approval_tier": result["tier"],
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

        self.push_screen(_CreateModal(prefixes), _on_dismiss)

    def action_answer(self) -> None:
        key = self._selected_key
        if key is None:
            return
        snap = snap_mod.load(self._home, key)
        if not snap.open_questions:
            self.notify("No open questions for this ticket", severity="warning")
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
                snap = snap_mod.load(self._home, key)
                self.query_one("#detail", Static).update(
                    _render_detail(snap, spec_tier(self._home, key)))
            return
        qid, text = questions[idx]
        remaining = len(questions) - idx
        position, total, body, recommend = ops_mod.parse_round_question(text)

        def _on_dismiss(answer: object) -> None:
            if answer is None:
                return
            if answer is _ACCEPT_ALL:
                # Queue the recommendation for every remaining question that has
                # one; keep walking (via modal, one at a time) only the ones that
                # don't -- fast-tracks the recommended ones without silently
                # skipping the ones that still need a typed answer.
                queued = 0
                unanswered: list[tuple[str, str]] = []
                for q_qid, q_text in questions[idx:]:
                    _, _, _, q_recommend = ops_mod.parse_round_question(q_text)
                    if q_recommend:
                        inbox.append_command(self._home, key, "ans",
                                             {"qid": q_qid, "text": q_recommend})
                        queued += 1
                    else:
                        unanswered.append((q_qid, q_text))
                if queued:
                    self.notify(f"{queued} recommendation(s) queued for {key}")
                self._walk_questions(key, unanswered, 0, answered + queued)
                return
            inbox.append_command(self._home, key, "ans", {"qid": qid, "text": answer})
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(
            _AnswerModal(key, qid, position, total, body, recommend, remaining, self._home),
            _on_dismiss,
        )

    def action_compact(self) -> None:
        key = self._selected_key
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

    def action_release(self) -> None:
        key = self._selected_key
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return

        def _on_confirm(ok: bool) -> None:
            if not ok:
                return
            claims.release(self._home, key)
            self.notify(f"Claim released for {key}")

        self.app.push_screen(
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

    def action_focus_detail(self) -> None:
        if self._selected_key:
            self.push_screen(DetailScreen(self._home, self._selected_key))

    def action_view_events(self) -> None:
        if self._selected_key:
            self.push_screen(EventsScreen(self._home, self._selected_key))

    def action_inbox_message(self) -> None:
        key = self._selected_key
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return

        def _on_dismiss(text: str | None) -> None:
            if text is None:
                return
            inbox.append_command(self._home, key, "msg", {"text": text})
            self.notify(f"Message queued for {key}")

        self.push_screen(_InboxModal(key), _on_dismiss)

    def action_view_logs(self) -> None:
        if self._selected_key:
            self.push_screen(LogsScreen(self._home, self._selected_key))

    def _populate(self) -> None:
        _name, predicate = _FILTERS[self._filter_idx]
        home = self._home

        # Load all rows once for counting and filtering
        all_rows = ticket_rows(home)

        # Snapshot-level state for filtering/toasting: the row tuples above
        # don't carry `.approved`, which the needs-you predicate needs.
        snaps_by_key = {row[-1]: snap_mod.load(home, row[-1]) for row in all_rows}

        # Detect tickets newly entering awaiting-human/degraded, OR newly
        # gated by the tier-2 approval gate (GA-21) -- the latter leaves
        # `phase` unchanged ("implementing"), so it's tracked as a second
        # signal per key rather than folded into the phase comparison.
        new_state = {key: (s.phase, needs_approval(home, key, s))
                     for key, s in snaps_by_key.items()}
        if self._prev_phases is not None:
            for key, (phase, gated) in new_state.items():
                prev_phase, prev_gated = self._prev_phases.get(key, (None, False))
                if phase in _NEEDS_YOU_PHASE_VALUES and prev_phase != phase:
                    self.notify(f"{key}: {phase}", severity="warning", timeout=6)
                elif gated and not prev_gated:
                    self.notify(f"{key}: needs-approval", severity="warning", timeout=6)
        self._prev_phases = new_state

        # Build filter bar: show counts per filter, bold the active one
        parts = []
        for i, (fname, fpred) in enumerate(_FILTERS):
            if fpred is None:
                count = len(all_rows)
            else:
                count = sum(1 for s in snaps_by_key.values() if fpred(home, s))
            label = f"{fname}({count})"
            if i == self._filter_idx:
                label = f"[reverse bold] {label} [/reverse bold]"
            else:
                label = f"[dim]{label}[/dim]"
            parts.append(label)
        self.query_one("#filter-bar", Static).update("  " + "  |  ".join(parts))

        # Apply current filter
        if predicate is not None:
            visible = [r for r in all_rows if predicate(home, snaps_by_key[r[-1]])]
        else:
            visible = all_rows

        table = self.query_one(DataTable)
        # Preserve cursor across clear/repopulate.
        prev_key: str | None = None
        try:
            rk = table.cursor_row_key
            if rk is not None and rk.value is not None:
                prev_key = str(rk.value)
        except Exception:
            pass
        prev_row = table.cursor_row
        table.clear()
        row_keys: list[str] = []
        for *cells, row_key in visible:
            table.add_row(*_styled_row(*cells), key=row_key)
            row_keys.append(row_key)
        if not row_keys:
            return
        if prev_key and prev_key in row_keys:
            table.move_cursor(row=row_keys.index(prev_key))
        else:
            table.move_cursor(row=min(prev_row, len(row_keys) - 1))
        if self._selected_key:
            self._refresh_events()

    def _refresh_events(self) -> None:
        if not self._selected_key:
            return
        events = event_log.read(self._home, self._selected_key)
        log = self.query_one("#events", RichLog)
        log.clear()
        for line in render_log(events, tail=self._tail_mode):
            log.write(line)


def main(args) -> int:
    from ..config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
