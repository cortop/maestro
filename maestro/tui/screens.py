"""Full-screen views pushed from the main board (events, logs, fleet, spec, …)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Markdown, RichLog, Static
from textual.worker import Worker, WorkerState

from .. import claims, config as config_mod, event_log, fleet as fleet_mod, inbox, snapshot as snap_mod, store
from ..dispatcher import schedule_status
from ..sessions import list_sessions
from .detail import render as _render_detail, render_pending as _render_pending
from .events import render_log, render_log_line
from .modals import _IntervalModal, _ScheduleModal
from .render import _fmt_epoch, _render_env, _render_fleet


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
            from .. import projection
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


class ProposalScreen(Screen):
    """Read-only viewer for a ticket's proposal.md."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "refresh_proposal", "Refresh"),
    ]

    CSS = "ProposalScreen #proposal-body { height: 1fr; }"

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="proposal-body"):
            yield Markdown("", id="proposal-md")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Proposal: {self._key}"
        self._refresh()

    def action_refresh_proposal(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        path = self._home / "tickets" / self._key / "proposal.md"
        text = path.read_text() if path.exists() else "(no proposal.md)"
        self.query_one("#proposal-md", Markdown).update(text)


class DetailScreen(Screen):
    """Full-screen right panel: ticket detail summary + event log."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("t", "toggle_tail", "Tail/Full"),
        ("r", "refresh", "Refresh"),
        ("p", "view_proposal", "Proposal"),
    ]

    CSS = """
    DetailScreen #ds-detail { height: auto; max-height: 14; padding: 0 1;
                               border-bottom: solid $primary; }
    DetailScreen #ds-events { height: 1fr; }
    """

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key
        self._tail_mode = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="ds-detail", markup=True)
        yield RichLog(id="ds-events", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._key
        self._refresh()

    def action_toggle_tail(self) -> None:
        self._tail_mode = not self._tail_mode
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def action_view_proposal(self) -> None:
        path = self._home / "tickets" / self._key / "proposal.md"
        if not path.exists():
            self.notify("No proposal.md for this ticket", severity="warning")
            return
        self.app.push_screen(ProposalScreen(self._home, self._key))

    def _refresh(self) -> None:
        snap = snap_mod.load(self._home, self._key)
        self.query_one("#ds-detail", Static).update(_render_detail(snap))
        events = event_log.read(self._home, self._key)
        log = self.query_one("#ds-events", RichLog)
        log.clear()
        for line in render_log(events, tail=self._tail_mode):
            log.write(line)


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


class ScheduleScreen(Screen):
    """View/add/edit/enable-disable config-declared `[[scheduled]]` tasks."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("n", "add_task", "Add"),
        ("e", "edit_task", "Edit"),
        ("t", "toggle_task", "Enable/Disable"),
        ("r", "refresh", "Refresh"),
    ]

    CSS = "ScheduleScreen #schedule-table { height: 1fr; }"

    def __init__(self, home: Path) -> None:
        super().__init__()
        self._home = home
        self._selected_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="schedule-table")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Scheduled tasks"
        table = self.query_one("#schedule-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Name")
        table.add_column("Every")
        table.add_column("Kind")
        table.add_column("Tier")
        table.add_column("Enabled")
        table.add_column("Last fired")
        table.add_column("Next due")
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        cfg = config_mod.load(str(self._home))
        rows = schedule_status(cfg, store.now_epoch())
        table = self.query_one("#schedule-table", DataTable)
        table.clear()
        for row in rows:
            table.add_row(
                row["name"], str(row["every"]), row["kind"], str(row["approval_tier"]),
                "yes" if row["enabled"] else "no",
                _fmt_epoch(row["last_fired"]), _fmt_epoch(row["next_due"]),
                key=row["name"],
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_name = str(event.row_key.value) if event.row_key and event.row_key.value is not None else None

    def _tasks(self, cfg: config_mod.Config) -> list[dict]:
        return list(cfg.scheduled)

    def action_add_task(self) -> None:
        def _on_dismiss(result: dict | None) -> None:
            if result is None:
                return
            cfg = config_mod.load(str(self._home))
            tasks = self._tasks(cfg)
            if any(t.get("name") == result["name"] for t in tasks):
                self.notify(f"A task named {result['name']!r} already exists", severity="warning")
                return
            tasks.append(result)
            config_mod.write_scheduled(self._home, tasks)
            self.notify(f"Added scheduled task {result['name']!r}")
            self._refresh()

        self.app.push_screen(_ScheduleModal(), _on_dismiss)

    def action_edit_task(self) -> None:
        if self._selected_name is None:
            self.notify("Select a task first", severity="warning")
            return
        cfg = config_mod.load(str(self._home))
        tasks = self._tasks(cfg)
        existing = next((t for t in tasks if t.get("name") == self._selected_name), None)
        if existing is None:
            self.notify("Task not found (config may have changed)", severity="warning")
            return

        def _on_dismiss(result: dict | None) -> None:
            if result is None:
                return
            new_tasks = [result if t.get("name") == existing.get("name") else t for t in tasks]
            config_mod.write_scheduled(self._home, new_tasks)
            self.notify(f"Updated scheduled task {result['name']!r}")
            self._refresh()

        self.app.push_screen(_ScheduleModal(existing), _on_dismiss)

    def action_toggle_task(self) -> None:
        if self._selected_name is None:
            self.notify("Select a task first", severity="warning")
            return
        cfg = config_mod.load(str(self._home))
        tasks = self._tasks(cfg)
        found = False
        for t in tasks:
            if t.get("name") == self._selected_name:
                t["enabled"] = not t.get("enabled", True)
                found = True
        if not found:
            self.notify("Task not found (config may have changed)", severity="warning")
            return
        config_mod.write_scheduled(self._home, tasks)
        self.notify(f"Toggled {self._selected_name}")
        self._refresh()
