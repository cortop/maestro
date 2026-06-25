"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Label


class MaestroTUI(App):
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = home

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"Home: {self._home}")
        yield Footer()


def main(args) -> int:
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
