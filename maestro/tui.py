"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from .projection import ticket_rows
from . import inbox, snapshot as snap_mod
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


class _CreateModal(ModalScreen):
    """Multi-field form to queue a new ticket; dismisses with a result dict or None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="create-dialog"):
            yield Label("[bold]New Ticket[/bold]")
            yield Label("Title [bold red]*[/bold red]")
            yield Input(placeholder="required", id="create-title")
            yield Label("Key")
            yield Input(placeholder="optional, e.g. FEAT-99", id="create-key")
            yield Label("Tier")
            yield Input(value="1", id="create-tier")
            yield Label("Priority")
            yield Input(value="3", id="create-priority")
            yield Label("Intent")
            yield Input(placeholder="optional", id="create-intent")
            yield Label("[dim]Tab/Enter → next · Enter on last → submit · Esc → cancel[/dim]")

    def on_mount(self) -> None:
        self.query_one("#create-title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        idx = inputs.index(event.input)
        if idx < len(inputs) - 1:
            inputs[idx + 1].focus()
        else:
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        title = self.query_one("#create-title", Input).value.strip()
        if not title:
            self.notify("Title is required", severity="warning")
            self.query_one("#create-title", Input).focus()
            return
        key_val = self.query_one("#create-key", Input).value.strip() or None
        tier_str = self.query_one("#create-tier", Input).value.strip() or "1"
        priority_str = self.query_one("#create-priority", Input).value.strip() or "3"
        intent_val = self.query_one("#create-intent", Input).value.strip() or None
        try:
            tier = int(tier_str)
            priority = int(priority_str)
        except ValueError:
            self.notify("Tier and priority must be integers", severity="warning")
            return
        self.dismiss({
            "title": title,
            "key": key_val,
            "tier": tier,
            "priority": priority,
            "intent": intent_val,
        })


class MaestroTUI(App):
    CSS = """
    #tickets { width: 2fr; height: 1fr; }
    #detail  { width: 1fr; height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("n", "create", "New"),
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

    def action_create(self) -> None:
        def _on_dismiss(result: dict | None) -> None:
            if result is None:
                return
            inbox.append_new(
                self._home,
                result["title"],
                result.get("key"),
                {
                    "approval_tier": result["tier"],
                    "priority": result["priority"],
                    "intent": result.get("intent"),
                },
            )
            self.notify("queued; dispatcher will mint the key")

        self.push_screen(_CreateModal(), _on_dismiss)

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
        for *cells, row_key in ticket_rows(self._home):
            table.add_row(*cells, key=row_key)
            row_keys.append(row_key)
        if not row_keys:
            return
        if prev_key and prev_key in row_keys:
            table.move_cursor(row=row_keys.index(prev_key))
        else:
            table.move_cursor(row=min(prev_row, len(row_keys) - 1))


def main(args) -> int:
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
