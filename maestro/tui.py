"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static

from .projection import ticket_rows
from . import inbox, snapshot as snap_mod
from .statemachine import Phase, ACTIVE_PHASES
from .tui_detail import render as _render_detail

_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})

# Named filters: (display_name, phase_set) — None phase_set means no filtering (show all)
_FILTERS: list[tuple[str, frozenset | None]] = [
    ("needs-you", _NEEDS_YOU_PHASES),
    ("active", ACTIVE_PHASES),
    ("all", None),
]


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
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("f", "cycle_filter", "Filter"),
    ]

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)
        self._selected_key: str | None = None
        self._filter_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="filter-bar")
        with Horizontal():
            yield DataTable(id="tickets")
            yield Static("[dim]Select a ticket[/dim]", id="detail")
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

    def action_cycle_filter(self) -> None:
        self._filter_idx = (self._filter_idx + 1) % len(_FILTERS)
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
                # Cancelled — stop walking; no state change
                return
            inbox.append_command(self._home, key, "ans", {"qid": qid, "text": answer})
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(_AnswerModal(key, qid, text, remaining), _on_dismiss)

    def _populate(self) -> None:
        _name, phases = _FILTERS[self._filter_idx]

        # Load all rows once for counting and filtering
        all_rows = ticket_rows(self._home)

        # Build filter bar: show counts per filter, bold the active one
        parts = []
        for i, (fname, fphases) in enumerate(_FILTERS):
            if fphases is None:
                count = len(all_rows)
            else:
                fvals = {p.value for p in fphases}
                count = sum(1 for r in all_rows if r[1] in fvals)
            label = f"{fname}({count})"
            if i == self._filter_idx:
                label = f"[bold]{label}[/bold]"
            parts.append(label)
        self.query_one("#filter-bar", Static).update("  " + "  |  ".join(parts))

        # Apply current filter
        if phases is not None:
            phase_vals = {p.value for p in phases}
            visible = [r for r in all_rows if r[1] in phase_vals]
        else:
            visible = all_rows

        table = self.query_one(DataTable)
        table.clear()
        for *cells, row_key in visible:
            table.add_row(*cells, key=row_key)


def main(args) -> int:
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
