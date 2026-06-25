"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static
from textual.worker import Worker, WorkerState

from .projection import ticket_rows
from . import fleet as fleet_mod, inbox, snapshot as snap_mod, store
from .tui_detail import render as _render_detail


class _AnswerModal(ModalScreen):
    """Single-question input modal; dismisses with the answer string or None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, key: str, qid: str, question_text: str, remaining: int) -> None:
        super().__init__()
        self._key = key
        self._qid = qid
        self._question_text = question_text
        self._remaining = remaining

    def compose(self) -> ComposeResult:
        header = f"[bold]{self._key}[/bold] ({self._remaining} remaining)"
        with Vertical(id="answer-dialog"):
            yield Label(header)
            yield Label(self._question_text, id="question-label")
            yield Input(placeholder="Answer (Enter to submit, Esc to cancel)", id="answer-input")

    def on_mount(self) -> None:
        self.query_one("#answer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _IntervalModal(ModalScreen):
    """Prompt for a dispatch interval (seconds) before calling fleet up."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="answer-dialog"):
            yield Label("[bold]Fleet up[/bold] — set dispatch interval")
            yield Input(placeholder="Interval in seconds (default: 300)", id="interval-input")

    def on_mount(self) -> None:
        self.query_one("#interval-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        try:
            interval = int(raw) if raw else 300
        except ValueError:
            interval = 300
        self.dismiss(interval)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _fmt_age(age_s: int | None) -> str:
    if age_s is None:
        return "never"
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    return f"{age_s // 3600}h ago"


def _render_fleet(status: dict, doctor: dict) -> str:
    loaded = "[green]yes[/green]" if status.get("loaded") else "[red]no[/red]"
    age = _fmt_age(status.get("heartbeat_age_s"))
    interval = status.get("interval")
    interval_str = f"{interval}s" if interval else "—"
    label = status.get("label", "—")
    dead = doctor.get("dead_letters", [])
    stale = doctor.get("stale", False)
    stale_str = "[yellow]yes[/yellow]" if stale else "no"
    dead_str = (", ".join(dead) if dead else "—")

    lines = [
        "[bold]Fleet & Health[/bold]",
        "",
        f"  Loaded:          {loaded}",
        f"  Heartbeat:       {age}",
        f"  Interval:        {interval_str}",
        f"  Label:           {label}",
        "",
        "[bold]Doctor[/bold]",
        "",
        f"  Stale:           {stale_str}",
        f"  Dead letters:    {dead_str}",
    ]
    return "\n".join(lines)


class FleetScreen(Screen):
    """Full-screen fleet & health panel."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("u", "fleet_up", "Up"),
        ("d", "fleet_down", "Down"),
        ("s", "dispatch_sweep", "Sweep"),
        ("p", "project_rebuild", "Project"),
        ("r", "refresh_status", "Refresh"),
    ]

    CSS = """
    FleetScreen #fleet-status { padding: 1 2; height: 1fr; }
    FleetScreen #fleet-log    { height: 8; border-top: solid $primary; padding: 0 1; }
    """

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._home = home
        self._status: dict = {}
        self._doctor: dict = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]Loading…[/dim]", id="fleet-status")
        yield Static("", id="fleet-log")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_worker()
        self.set_interval(5.0, self._refresh_worker)

    # --- workers (run in threads so the event loop stays free) ---------------

    def _refresh_worker(self) -> None:
        self.run_worker(self._load_status, thread=True, group="refresh", exclusive=True,
                        name="fleet-refresh")

    def _load_status(self) -> tuple[dict, dict]:
        status = fleet_mod.status(self._home)
        hb = store.read_json(self._home / "derived" / ".heartbeat.json", {})
        age = round(store.now_epoch() - hb["epoch"]) if hb.get("epoch") else None
        dead = list((self._home / "tickets" / "_deadletter").glob("*.md")) \
            if (self._home / "tickets" / "_deadletter").exists() else []
        doctor = {
            "heartbeat_age_s": age,
            "dead_letters": [p.stem for p in dead],
            "stale": age is not None and age > 1800,
        }
        return status, doctor

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "fleet-refresh" and event.state == WorkerState.SUCCESS:
            self._status, self._doctor = event.worker.result
            self.query_one("#fleet-status", Static).update(
                _render_fleet(self._status, self._doctor)
            )

    # --- key actions ---------------------------------------------------------

    def action_refresh_status(self) -> None:
        self._refresh_worker()

    def action_fleet_up(self) -> None:
        def _on_interval(interval: int | None) -> None:
            if interval is None:
                return
            self.run_worker(
                lambda: fleet_mod.up(self._home, interval=interval),
                thread=True, name="fleet-up",
            )
            self._log(f"fleet up --interval {interval} … ")
            self._refresh_worker()

        self.app.push_screen(_IntervalModal(), _on_interval)

    def action_fleet_down(self) -> None:
        self.run_worker(lambda: fleet_mod.down(self._home), thread=True, name="fleet-down")
        self._log("fleet down … ")
        self._refresh_worker()

    def action_dispatch_sweep(self) -> None:
        self._log("dispatching (dry-run) … ")
        self.run_worker(self._run_dispatch, thread=True, name="dispatch-sweep")

    def _run_dispatch(self) -> str:
        try:
            p = subprocess.run(
                ["maestro", "--home", str(self._home), "dispatch", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            return (p.stdout or p.stderr or "done").strip()
        except Exception as exc:
            return str(exc)

    def action_project_rebuild(self) -> None:
        self._log("rebuilding projection … ")
        self.run_worker(self._run_project, thread=True, name="project-rebuild")

    def _run_project(self) -> str:
        try:
            from . import projection
            written = projection.write(self._home)
            return f"wrote {len(written)} files"
        except Exception as exc:
            return str(exc)

    def _log(self, msg: str) -> None:
        w = self.query_one("#fleet-log", Static)
        current = str(w.renderable)
        lines = (current + "\n" + msg).strip().splitlines()
        w.update("\n".join(lines[-6:]))


class MaestroTUI(App):
    CSS = """
    #tickets { width: 2fr; height: 1fr; }
    #detail  { width: 1fr; height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("f", "fleet_panel", "Fleet"),
    ]

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)
        self._selected_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="tickets")
            yield Static(f"[dim]Select a ticket[/dim]", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Key", "Phase", "Title", "PR", "CI", "Tier", "Fails")
        self._populate()
        self.set_interval(3.0, self._populate)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None
        self._selected_key = key
        detail = self.query_one("#detail", Static)
        if key is None:
            detail.update(f"[dim]Select a ticket[/dim]")
            return
        snap = snap_mod.load(self._home, key)
        detail.update(_render_detail(snap))

    def action_refresh(self) -> None:
        self._populate()

    def action_fleet_panel(self) -> None:
        self.push_screen(FleetScreen(self._home))

    def action_answer(self) -> None:
        key = self._selected_key
        if key is None:
            return
        snap = snap_mod.load(self._home, key)
        if not snap.open_questions:
            self.notify("No open questions for this ticket", severity="warning")
            return
        self._walk_questions(key, list(snap.open_questions.items()), 0, 0)

    def _walk_questions(
        self, key: str, questions: list[tuple[str, str]], idx: int, answered: int
    ) -> None:
        if idx >= len(questions):
            if answered:
                self.notify(f"{answered} answer(s) queued for {key}")
                snap = snap_mod.load(self._home, key)
                self.query_one("#detail", Static).update(_render_detail(snap))
            return
        qid, text = questions[idx]
        remaining = len(questions) - idx

        def _on_dismiss(answer: str | None) -> None:
            if answer is None:
                # Cancelled — stop walking; no state change
                return
            inbox.append_command(self._home, key, "ans", {"qid": qid, "text": answer})
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(_AnswerModal(key, qid, text, remaining), _on_dismiss)

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for *cells, row_key in ticket_rows(self._home):
            table.add_row(*cells, key=row_key)


def main(args) -> int:
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
