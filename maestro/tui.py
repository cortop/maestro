"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from .projection import ticket_rows
from . import event_log, inbox, snapshot as snap_mod
from .tui_detail import render as _render_detail
from .tui_events import render_log


class EventsScreen(Screen):
    """Full-screen scrollable event timeline for one ticket."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("t", "toggle_tail", "Tail/Full"),
    ]

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key
        self._tail_mode = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="events-full", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Events: {self._key}"
        self._refresh()

    def action_toggle_tail(self) -> None:
        self._tail_mode = not self._tail_mode
        self._refresh()

    def _refresh(self) -> None:
        log = self.query_one("#events-full", RichLog)
        events = event_log.read(self._home, self._key)
        log.clear()
        for line in render_log(events, tail=self._tail_mode):
            log.write(line)


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


class MaestroTUI(App):
    CSS = """
    #tickets { width: 2fr; height: 1fr; }
    #detail  { width: 1fr; height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("t", "toggle_tail", "Tail/Full"),
        ("enter", "view_events", "Events"),
    ]

    _selected_key: str | None = None
    _tail_mode: bool = True  # default: show tail in the sidebar panel

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)
        self._selected_key: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="tickets")
            with Vertical(id="right"):
                yield Static("[dim]Select a ticket[/dim]", id="detail")
                yield RichLog(id="events", highlight=True, markup=True)
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
            detail.update("[dim]Select a ticket[/dim]")
            self.query_one("#events", RichLog).clear()
            return
        snap = snap_mod.load(self._home, key)
        detail.update(_render_detail(snap))
        self._refresh_events()

    def action_refresh(self) -> None:
        self._populate()

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
                return
            inbox.append_command(self._home, key, "ans", {"qid": qid, "text": answer})
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(_AnswerModal(key, qid, text, remaining), _on_dismiss)

    def action_toggle_tail(self) -> None:
        self._tail_mode = not self._tail_mode
        self._refresh_events()

    def action_view_events(self) -> None:
        if self._selected_key:
            self.push_screen(EventsScreen(self._home, self._selected_key))

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for *cells, row_key in ticket_rows(self._home):
            table.add_row(*cells, key=row_key)
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
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
