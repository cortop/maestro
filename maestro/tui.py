"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from .projection import ticket_rows
from . import inbox, snapshot as snap_mod
from .statemachine import Phase
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


class _CmdModal(ModalScreen):
    """Command palette modal; dismisses with (command, args_text) or None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, key: str, phase: str) -> None:
        super().__init__()
        self._key = key
        self._phase = phase

    def compose(self) -> ComposeResult:
        is_degraded = self._phase == Phase.DEGRADED.value
        header = f"[bold]{self._key}[/bold] — {self._phase}"
        with Vertical(id="cmd-dialog"):
            yield Label(header)
            if is_degraded:
                yield Label(
                    "[dim]degraded:[/dim] type [bold]retry[/bold] or [bold]discard[/bold]",
                    id="cmd-hint",
                )
            yield Input(
                placeholder="command (e.g. retry, discard)  [Enter to send, Esc to cancel]",
                id="cmd-input",
            )
            yield Input(
                placeholder="args text (optional)",
                id="cmd-args",
            )

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cmd-input":
            self.query_one("#cmd-args", Input).focus()
            return
        # args field submitted — send
        self._submit()

    def _submit(self) -> None:
        command = self.query_one("#cmd-input", Input).value.strip()
        args_text = self.query_one("#cmd-args", Input).value.strip()
        if command:
            self.dismiss((command, args_text))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MaestroTUI(App):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("c", "cmd", "Command"),
        ("ctrl+r", "retry", "Retry"),
        ("ctrl+d", "discard", "Discard"),
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

    def action_cmd(self) -> None:
        key = self._selected_key
        if key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        snap = snap_mod.load(self._home, key)

        def _on_dismiss(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            command, args_text = result
            args = {"text": args_text} if args_text else {}
            inbox.append_command(self._home, key, command, args)
            self.notify(f"'{command}' queued for {key}")

        self.push_screen(_CmdModal(key, snap.phase), _on_dismiss)

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
