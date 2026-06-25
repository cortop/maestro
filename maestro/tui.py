"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from .projection import ticket_rows
from . import snapshot as snap_mod
from .tui_detail import render as _render_detail


class MaestroTUI(App):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)

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
        detail = self.query_one("#detail", Static)
        if key is None:
            detail.update(f"[dim]Select a ticket[/dim]")
            return
        snap = snap_mod.load(self._home, key)
        detail.update(_render_detail(snap))

    def action_refresh(self) -> None:
        self._populate()

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
