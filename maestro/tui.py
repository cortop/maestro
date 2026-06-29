"""Interactive TUI for maestro — requires the `tui` extra (textual)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, Markdown, RichLog, Static, TextArea
from textual.worker import Worker, WorkerState

from .projection import ticket_rows
from . import claims, config as config_mod, event_log, fleet as fleet_mod, inbox, ops as ops_mod, snapshot as snap_mod, store
from .config import Config
from .sessions import list_sessions
from .statemachine import Phase, ACTIVE_PHASES
from .tui_detail import render as _render_detail, render_pending as _render_pending
from .tui_events import render_log, render_log_line


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

_NEEDS_YOU_PHASES = frozenset({Phase.AWAITING_HUMAN, Phase.DEGRADED})

# Named filters: (display_name, phase_set) — None phase_set means no filtering (show all)
_FILTERS: list[tuple[str, frozenset | None]] = [
    ("needs-you", _NEEDS_YOU_PHASES),
    ("active", ACTIVE_PHASES),
    ("all", None),
]

# Phase → Rich style string for row coloring
_PHASE_STYLE: dict[str, str] = {
    Phase.IMPLEMENTING.value:   "green",
    Phase.AWAITING_CI.value:    "cyan",
    Phase.IN_REVIEW.value:      "cyan",
    Phase.AWAITING_HUMAN.value: "yellow bold",
    Phase.DEGRADED.value:       "red bold",
    Phase.DONE.value:           "dim",
    Phase.TERMINATING.value:    "dim",
}


def _styled_row(*cells: str) -> tuple:
    """Return cells styled uniformly by the phase (cells[1])."""
    style = _PHASE_STYLE.get(cells[1], "")
    if not style:
        return cells
    return tuple(Text(str(c), style=style) for c in cells)


class LogsScreen(Screen):
    """Screen that tails the live session log for one ticket."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key
        self._stop = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="logs-view", highlight=False, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Logs: {self._key}"
        self.run_worker(self._tail, thread=True, name="tail-logs")

    def on_unmount(self) -> None:
        self._stop = True

    def _tail(self) -> None:
        log_widget = self.query_one("#logs-view", RichLog)

        claim = claims.read_claim(self._home, self._key)
        live_pid = claim.get("pid") if claim else None
        log_path_str = claim.get("log_path") if claim else None

        if log_path_str:
            log_path = Path(log_path_str)
        else:
            sessions_list = list_sessions(self._home, self._key)
            if not sessions_list:
                self.app.call_from_thread(log_widget.write, "(no session logs found)")
                return
            log_path = Path(sessions_list[0]["path"])

        if not log_path.exists():
            self.app.call_from_thread(log_widget.write, f"(log not found: {log_path.name})")
            return

        is_stream = log_path.name.endswith(".stream.jsonl")

        with log_path.open(encoding="utf-8", errors="replace") as f:
            buf = ""
            while not self._stop:
                chunk = f.read(4096)
                if chunk:
                    buf += chunk
                    if is_stream:
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            for rendered in render_log_line(obj):
                                self.app.call_from_thread(log_widget.write, rendered)
                    else:
                        self.app.call_from_thread(log_widget.write, chunk)
                else:
                    if live_pid and not claims.pid_alive(live_pid):
                        break
                    if not live_pid:
                        break
                    time.sleep(0.25)


class _AnswerModal(ModalScreen):
    """Single-question input modal; dismisses with the answer string or None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    _AnswerModal {
        align: center middle;
    }
    #answer-dialog {
        width: 80%;
        max-height: 85%;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    #spec-scroll {
        max-height: 12;
        border: solid $panel;
        margin-bottom: 1;
    }
    #question-scroll {
        max-height: 8;
        margin-bottom: 1;
    }
    """

    def __init__(self, key: str, qid: str, question_text: str, remaining: int, home: Path) -> None:
        super().__init__()
        self._key = key
        self._qid = qid
        self._question_text = question_text
        self._remaining = remaining
        self._home = home

    def compose(self) -> ComposeResult:
        header = f"[bold]{self._key}[/bold] ({self._remaining} remaining)"
        spec_path = self._home / "tickets" / self._key / "spec.md"
        spec_text = spec_path.read_text() if spec_path.exists() else ""
        with Vertical(id="answer-dialog"):
            yield Label(header)
            if spec_text:
                yield Label("[dim]── Spec ──[/dim]")
                with VerticalScroll(id="spec-scroll"):
                    yield Static(spec_text, markup=False)
            yield Label("[dim]── Question ──[/dim]")
            with VerticalScroll(id="question-scroll"):
                yield Static(self._question_text, markup=False)
            yield Input(placeholder="Answer (Enter to submit, Esc to cancel)", id="answer-input")

    def on_mount(self) -> None:
        self.query_one("#answer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


# Phase-aware command reference shown in _CmdModal.
_PHASE_COMMANDS: dict[str, list[tuple[str, str]]] = {
    Phase.DEGRADED.value: [
        ("retry", "re-enter implementing"),
        ("discard", "drop this ticket permanently"),
    ],
    Phase.AWAITING_HUMAN.value: [
        ("ans <qid> <text>", "answer the open question"),
        ("approve", "approve and advance"),
        ("yes", "shorthand approve"),
        ("no", "reject the plan"),
        ("reject", "reject and discard"),
        ("discard", "drop this ticket permanently"),
    ],
}
_DEFAULT_COMMANDS: list[tuple[str, str]] = [
    ("retry", "re-enter implementing"),
    ("discard", "drop this ticket permanently"),
    ("requeue <secs>", "delay next reconcile by N seconds"),
]


class _CmdModal(ModalScreen):
    """Command palette modal; dismisses with (command, args_text) or None on cancel."""

    DEFAULT_CSS = """
    _CmdModal {
        align: center middle;
    }
    #cmd-dialog {
        width: 70%;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, key: str, phase: str) -> None:
        super().__init__()
        self._key = key
        self._phase = phase

    def compose(self) -> ComposeResult:
        header = f"[bold]{self._key}[/bold] — {self._phase}"
        commands = _PHASE_COMMANDS.get(self._phase, _DEFAULT_COMMANDS)
        with Vertical(id="cmd-dialog"):
            yield Label(header)
            yield Label("[dim]── Commands ──[/dim]")
            for cmd, desc in commands:
                yield Label(
                    f"  [bold]{cmd}[/bold]  [dim]{desc}[/dim]",
                    classes="cmd-row",
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


class _CreateModal(ModalScreen):
    """Multi-field form to queue a new ticket; dismisses with a result dict or None on cancel."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+enter", "submit", "Submit"),
    ]

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
            yield TextArea(id="create-intent")
            yield Label("[dim]Tab/Enter → next · Ctrl+Enter → submit · Esc → cancel[/dim]")

    def on_mount(self) -> None:
        self.query_one("#create-title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        idx = inputs.index(event.input)
        if idx < len(inputs) - 1:
            inputs[idx + 1].focus()
        else:
            self.query_one("#create-intent", TextArea).focus()

    def action_submit(self) -> None:
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
        intent_val = self.query_one("#create-intent", TextArea).text.strip() or None
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


class _ConfirmModal(ModalScreen):
    """Simple yes/no confirmation modal; dismisses with True on confirm, False on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="answer-dialog"):
            yield Label(self._message)
            yield Label("[dim]y / Enter → confirm · n / Esc → cancel[/dim]")
            yield Input(placeholder="y to confirm", id="confirm-input")

    def on_mount(self) -> None:
        self.query_one("#confirm-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip().lower()
        self.dismiss(val in ("y", "yes", ""))

    def on_key(self, event) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key == "n":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


def _fmt_age(age_s: int | None) -> str:
    if age_s is None:
        return "never"
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    return f"{age_s // 3600}h ago"


def _render_badge(status: dict) -> str:
    """Compact fleet state for the header: on/off, interval, heartbeat age."""
    dot = "[green]●[/green] up" if status.get("loaded") else "[red]○[/red] down"
    interval = status.get("interval")
    interval_str = f"{interval}s" if interval else "—"
    hb = _fmt_age(status.get("heartbeat_age_s"))
    return f"{dot}  [dim]int[/dim] {interval_str}  [dim]hb[/dim] {hb} "


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
        self._log_lines: list[str] = []

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
        if event.state == WorkerState.SUCCESS:
            if event.worker.name == "fleet-refresh":
                self._status, self._doctor = event.worker.result
                self.query_one("#fleet-status", Static).update(
                    _render_fleet(self._status, self._doctor)
                )
            elif event.worker.name in ("dispatch-sweep", "project-rebuild"):
                self._log(str(event.worker.result))
        elif event.state == WorkerState.ERROR:
            self._log(f"[red]{event.worker.name} failed: {event.worker.error}[/red]")

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
        self._log_lines.append(msg)
        del self._log_lines[:-6]
        self.query_one("#fleet-log", Static).update("\n".join(self._log_lines))


class SpecScreen(Screen):
    """Full-screen spec viewer + pending inbox for one ticket."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit_spec", "Edit"),
        ("r", "refresh_spec", "Refresh"),
    ]

    CSS = """
    SpecScreen #spec-body   { height: 1fr; }
    SpecScreen #spec-pending { height: auto; max-height: 8;
                               border-top: solid $primary; padding: 0 1; }
    """

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="spec-body"):
            yield Markdown("", id="spec-md")
        yield Static("", id="spec-pending", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Spec: {self._key}"
        self._refresh()

    def _refresh(self) -> None:
        spec_path = self._home / "tickets" / self._key / "spec.md"
        spec_text = spec_path.read_text() if spec_path.exists() else "(no spec)"
        pending_cmds = inbox.pending(self._home, self._key)
        self.query_one("#spec-md", Markdown).update(spec_text)
        pending_markup = _render_pending(pending_cmds)
        self.query_one("#spec-pending", Static).update(
            f"[bold]Pending inbox[/bold]  {pending_markup}"
        )

    def action_edit_spec(self) -> None:
        import os
        spec_path = self._home / "tickets" / self._key / "spec.md"
        editor = os.environ.get("EDITOR", "vi")
        with self.app.suspend():
            subprocess.run([editor, str(spec_path)])
        self._refresh()

    def action_refresh_spec(self) -> None:
        self._refresh()


def _render_env(cfg: config_mod.Config) -> str:
    toml_path = config_mod.config_path(cfg.home)
    toml_exists = "[dim](exists)[/dim]" if toml_path.exists() else "[dim](not found)[/dim]"
    providers = cfg.providers
    lines = [
        "[bold]Config / Environment[/bold]",
        "",
        f"  home:               {cfg.home}",
        f"  config.toml:        {toml_path}  {toml_exists}",
        f"  repo_path:          {cfg.repo_path or '—'}",
        f"  branch_prefix:      {cfg.branch_prefix}",
        f"  reconcile_command:  {cfg.reconcile_command}",
        f"  max_concurrency:    {cfg.max_concurrency}",
        f"  max_impl_turns:     {cfg.max_impl_turns}",
        "",
        "[bold]Providers[/bold]",
        "",
    ]
    for k, v in providers.items():
        lines.append(f"  {k + ':':20} {v}")
    return "\n".join(lines)


class EnvScreen(Screen):
    """Read-only panel showing resolved config — same values as `maestro env`."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    CSS = "EnvScreen #env-panel { padding: 1 2; height: 1fr; }"

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._home = home

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]Loading…[/dim]", id="env-panel")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Env"
        cfg = config_mod.load(str(self._home))
        self.query_one("#env-panel", Static).update(_render_env(cfg))


class MaestroTUI(App):
    CSS = """
    Screen { layers: base topbar; }
    Header { layer: base; }
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
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "answer", "Answer"),
        ("c", "cmd", "Command"),
        ("ctrl+r", "retry", "Retry"),
        ("ctrl+d", "discard", "Discard"),
        ("f", "cycle_filter", "Filter"),
        ("F", "fleet_panel", "Fleet"),
        ("e", "env_panel", "Env"),
        ("n", "create", "New"),
        ("s", "show_spec", "Spec"),
        ("t", "toggle_tail", "Tail/Full"),
        ("enter", "view_events", "Events"),
        ("x", "compact", "Compact"),
        ("z", "release", "Release"),
        ("p", "project_rebuild", "Project"),
        ("l", "view_logs", "Logs"),
    ]

    _selected_key: str | None = None
    _tail_mode: bool = True  # default: show tail in the sidebar panel

    def __init__(self, home: str) -> None:
        super().__init__()
        self._home = Path(home)
        self._selected_key: str | None = None
        self._filter_idx: int = 0
        self._prev_phases: dict[str, str] | None = None  # None = first poll (no notifications)

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
        table.add_columns("Key", "Phase", "Title", "PR", "CI", "Tier", "Fails")
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
        detail.update(_render_detail(snap))
        self._refresh_events()

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

    def action_fleet_panel(self) -> None:
        self.push_screen(FleetScreen(self._home))

    def action_show_spec(self) -> None:
        if self._selected_key is None:
            self.notify("Select a ticket first", severity="warning")
            return
        self.push_screen(SpecScreen(self._home, self._selected_key))

    def action_env_panel(self) -> None:
        self.push_screen(EnvScreen(self._home))

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
                return
            inbox.append_command(self._home, key, "ans", {"qid": qid, "text": answer})
            self._walk_questions(key, questions, idx + 1, answered + 1)

        self.push_screen(_AnswerModal(key, qid, text, remaining, self._home), _on_dismiss)

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
            from . import projection
            written = projection.write(self._home)
            return f"Wrote {len(written)} projection files"
        except Exception as exc:
            return str(exc)

    def action_toggle_tail(self) -> None:
        self._tail_mode = not self._tail_mode
        self._refresh_events()

    def action_view_events(self) -> None:
        if self._selected_key:
            self.push_screen(EventsScreen(self._home, self._selected_key))

    def action_view_logs(self) -> None:
        if self._selected_key:
            self.push_screen(LogsScreen(self._home, self._selected_key))

    def _populate(self) -> None:
        _name, phases = _FILTERS[self._filter_idx]

        # Load all rows once for counting and filtering
        all_rows = ticket_rows(self._home)

        # Detect tickets newly entering awaiting-human / degraded and toast once.
        new_phases = {row[-1]: row[1] for row in all_rows}  # row_key (last) -> phase (index 1)
        if self._prev_phases is not None:
            _notify_phases = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value}
            for key, phase in new_phases.items():
                if phase in _notify_phases and self._prev_phases.get(key) != phase:
                    self.notify(f"{key}: {phase}", severity="warning", timeout=6)
        self._prev_phases = new_phases

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
    from .config import load
    cfg = load(getattr(args, "home", None))
    MaestroTUI(home=str(cfg.home)).run()
    return 0
