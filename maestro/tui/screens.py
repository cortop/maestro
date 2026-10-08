"""Full-screen views pushed from the main board (events, logs, fleet, spec, …)."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult, SuspendNotSupported
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Markdown, RichLog, Static, Tree
from textual.worker import Worker, WorkerState

from .. import claims, config as config_mod, depgraph, event_log, fleet as fleet_mod, health, inbox, ops, ratelimit, snapshot as snap_mod, store
from ..dispatcher import schedule_status, spec_runner
from ..sessions import list_sessions
from .detail import render as _render_detail, render_pending as _render_pending
from .events import render_inbox, render_log, render_log_line, render_opencode_log_line, render_pi_log_line
from .modals import _ConfirmModal, _IntervalModal, _ScheduleModal
from .render import _dep_label, _fmt_epoch, _render_dep_header, _render_env, _render_fleet


def editor_argv(path: Path) -> list[str]:
    """`$VISUAL`, then `$EDITOR`, then `vi`, shlex-split, with `path` appended."""
    cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return [*shlex.split(cmd), str(path)]


def edit_in_editor(app, path: Path) -> str | None:
    """Open `path` in the user's editor, suspending the TUI while it runs.

    Returns None on success, else a human-readable warning (never raises for a
    missing file/editor or a terminal that can't be suspended).
    """
    if not path.is_file():
        return f"Spec not found: {path}"
    try:
        argv = editor_argv(path)
    except ValueError as exc:
        return f"Bad editor command: {exc}"
    if not argv[:-1]:
        return "Editor command is empty"
    try:
        with app.suspend():
            subprocess.run(argv)
    except SuspendNotSupported:
        return "This terminal can't be suspended to run an editor"
    except FileNotFoundError:
        return f"Editor not found: {argv[0]}"
    return None


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


class InboxScreen(Screen):
    """Full-screen listing of every inbox entry for one ticket (T-114) --
    including already-processed ones, with pending/processed visibly distinct.
    Read-only: derives the split from the machine-owned cursor, never writes."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="inbox-full", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Inbox: {self._key}"
        self._refresh()

    def _refresh(self) -> None:
        log = self.query_one("#inbox-full", RichLog)
        log.clear()
        entries = store.read_jsonl(store.inbox_path(self._home, self._key))
        if not entries:
            log.write("(inbox empty)")
            return
        cursor = inbox._cursor(store.cursor_path(self._home, self._key))
        for line in render_inbox(entries, cursor):
            log.write(line)


def _is_error_line(text: str) -> bool:
    """Rendered log lines that mark an error / rate-limit (see ``render_*_log_line``)."""
    return text.startswith("[red]") or text.startswith("[yellow]── rate_limit")


class LogsScreen(Screen):
    """Live session console for one ticket: sticky summary header, follow toggle, session
    picker, auto-advance across claim hand-offs, and a jump to the next error."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("f", "toggle_follow", "Follow"),
        ("s", "pick_session", "Session"),
        ("e", "next_error", "Next error"),
    ]

    DEFAULT_CSS = """
    LogsScreen #logs-summary { dock: top; height: 1; padding: 0 1; background: $boost; }
    """

    def __init__(self, home: Path, key: str) -> None:
        super().__init__()
        self._home = home
        self._key = key
        self._stop = False
        self._gen = 0
        self._cur_path: Path | None = None
        self._err_lines: list[int] = []
        self._last_jump = -1
        self._summary_cache: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="logs-summary")
        yield RichLog(id="logs-view", highlight=False, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Logs: {self._key}"
        self._update_header()
        self.set_interval(1.0, self._update_header)
        self._start_tail()

    def on_unmount(self) -> None:
        self._stop = True

    def _start_tail(self, pick: dict | None = None) -> None:
        self._gen += 1
        gen = self._gen
        self.run_worker(lambda: self._tail(gen, pick), thread=True, name="tail-logs", exclusive=True)

    # ---- main-thread helpers -------------------------------------------------

    def _append(self, gen: int, text: str) -> None:
        if gen != self._gen:
            return
        log = self.query_one("#logs-view", RichLog)
        if _is_error_line(text):
            self._err_lines.append(len(log.lines))
        log.write(text)

    def _set_current(self, gen: int, path: Path) -> None:
        if gen != self._gen:
            return
        self._cur_path = path
        self._summary_cache = None
        self._update_header()

    def _summary_text(self) -> Text:
        from .. import steplog
        path = self._cur_path
        if path is None:
            return Text("(no session)", style="dim")
        try:
            st = path.stat()
        except OSError:
            return Text(f"(log not found: {path.name})", style="dim")
        stamp = (path, st.st_mtime_ns, st.st_size)
        if self._summary_cache is None or self._summary_cache[0] != stamp:
            cost = turns = dur = denials = None
            tokens = None
            try:
                if path.name.endswith(".stream.jsonl"):
                    s = steplog.result_summary(o for _, o in steplog.iter_records(path))
                    cost, turns, dur, denials, tokens = (
                        s["cost"], s["turns"], s["duration_ms"], s["denials"], s["tokens"])
                    outcome = s["outcome"]
                else:
                    info = steplog.session_outcome(path)
                    outcome = info["outcome"]
                    res = info.get("result")
                    if isinstance(res, dict):
                        turns = res.get("num_turns")
            except OSError:
                outcome = "unknown"
            self._summary_cache = (stamp, outcome, cost, turns, dur, denials, tokens)
        _, outcome, cost, turns, dur, denials, tokens = self._summary_cache
        parts = [
            outcome,
            f"${cost:.2f}" if isinstance(cost, (int, float)) else "$—",
            f"{turns if turns is not None else '—'} turns",
            f"{denials if denials is not None else '—'} denied",
            f"{dur / 1000:.1f}s" if isinstance(dur, (int, float)) else "—",
        ]
        if outcome == "running":
            if tokens:
                parts.append(f"{tokens} tok")
            parts.append(f"last write {max(0, int(time.time() - st.st_mtime))}s ago")
        text = " · ".join(parts)
        if not self.query_one("#logs-view", RichLog).auto_scroll:
            text += " · paused"
        bad = outcome in ("error", "rate_limited", "crashed")
        return Text(text, style="bold red" if bad else "")

    def _update_header(self) -> None:
        try:
            self.query_one("#logs-summary", Static).update(self._summary_text())
        except Exception:  # not mounted yet / already gone
            pass

    # ---- actions -------------------------------------------------------------

    def action_toggle_follow(self) -> None:
        log = self.query_one("#logs-view", RichLog)
        log.auto_scroll = not log.auto_scroll
        if log.auto_scroll:
            log.scroll_end(animate=False)
        self._update_header()

    def action_next_error(self) -> None:
        log = self.query_one("#logs-view", RichLog)
        floor = max(int(log.scroll_y), self._last_jump)
        nxt = next((o for o in self._err_lines if o > floor), None)
        if nxt is None:
            self.notify("No further errors", severity="information")
            return
        self._last_jump = nxt
        log.auto_scroll = False  # else the next write snaps back to the bottom
        log.scroll_to(y=nxt, animate=False)
        self._update_header()

    def action_pick_session(self) -> None:
        from .modals import _SessionPickModal
        sessions = list_sessions(self._home, self._key, with_outcome=True)
        if not sessions:
            self.notify("No sessions", severity="warning")
            return
        labels = [f"{s['ts']} {s['format']} {s.get('outcome', '?')} {s.get('model', '?')}"
                  for s in sessions]

        def _picked(idx) -> None:
            if idx is None:
                return
            log = self.query_one("#logs-view", RichLog)
            log.clear()
            self._err_lines = []
            self._last_jump = -1
            self._start_tail(pick=sessions[idx])

        self.app.push_screen(_SessionPickModal(labels), _picked)

    # ---- tail worker (thread) ------------------------------------------------

    def _live(self, gen: int) -> bool:
        return not self._stop and gen == self._gen

    def _emit(self, gen: int, text: str) -> None:
        if self._live(gen):
            self.app.call_from_thread(self._append, gen, text)

    def _render_history(self, gen: int, sessions_list: list[dict],
                        skip_path: Path | None) -> None:
        """T-119: render every captured session oldest-first under a header, one file at a
        time. *skip_path* (the live session) is left for the tail loop to render."""
        from .. import steplog
        for sess in reversed(sessions_list):
            path = Path(sess["path"])
            if not self._live(gen) or (skip_path is not None and path == skip_path):
                continue
            if not path.exists():
                self._emit(gen, f"(session {sess['session_id']}: log file missing, skipped)")
                continue
            try:
                outcome = steplog.session_outcome(path)["outcome"]
                self._emit(gen, _session_header(sess, outcome, steplog.session_model(path)))
                self._render_file(gen, path)
            except OSError:
                self._emit(gen, f"(session {sess['session_id']}: log file missing, skipped)")

    def _render_file(self, gen: int, path: Path) -> None:
        is_pi = path.name.endswith(".pi.jsonl")
        is_opencode = path.name.endswith(".opencode.jsonl")
        structured = is_pi or is_opencode or path.name.endswith(".stream.jsonl")
        render_line = (render_pi_log_line if is_pi
                       else render_opencode_log_line if is_opencode
                       else render_log_line)
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                if not self._live(gen):
                    return
                if not structured:
                    self._emit(gen, raw)
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for rendered in render_line(obj):
                    self._emit(gen, rendered)

    def _tail(self, gen: int, pick: dict | None = None) -> None:
        from .. import steplog
        if pick is not None:
            path = Path(pick["path"])
            self.app.call_from_thread(self._set_current, gen, path)
            if not path.exists():
                self._emit(gen, f"(log not found: {path.name})")
                return
            self._emit(gen, _session_header(pick, pick.get("outcome") or steplog.session_outcome(path)["outcome"],
                                            steplog.session_model(path)))
            self._render_file(gen, path)
            self.app.call_from_thread(self._set_current, gen, path)
            return

        claim = claims.read_claim(self._home, self._key)
        live_pid = claim.get("pid") if claim else None
        log_path_str = claim.get("log_path") if claim else None
        verdict = claims.verify_claim(self._home, self._key) if claim else "unknown"

        sessions_list = list_sessions(self._home, self._key)
        if log_path_str:
            log_path = Path(log_path_str)
        else:
            if not sessions_list:
                self._emit(gen, "(no session logs found)")
                return
            log_path = Path(sessions_list[0]["path"])

        # Every earlier session first (oldest-first), then the live one is tailed below.
        self._render_history(gen, sessions_list, skip_path=log_path)

        if not log_path.exists():
            self._emit(gen, f"(log not found: {log_path.name})")
            return

        live = next((x for x in sessions_list if Path(x["path"]) == log_path), None)
        if live is not None:
            self._emit(gen, _session_header(live, steplog.session_outcome(log_path)["outcome"],
                                            steplog.session_model(log_path)))
        self.app.call_from_thread(self._set_current, gen, log_path)

        self._follow(gen, log_path, live_pid, verdict)
        # Auto-advance (read-only: read_claim/verify_claim never release a claim): keep
        # polling for a hand-off once a live session we were tailing ends.
        if verdict == "denied" or not live_pid:
            return
        cur = log_path
        while self._live(gen):
            time.sleep(0.5)
            claim = claims.read_claim(self._home, self._key)
            new = claim.get("log_path") if claim else None
            if not new or Path(new) == cur or not Path(new).exists():
                continue
            if claims.verify_claim(self._home, self._key) == "denied":
                continue
            cur = Path(new)
            phase = snap_mod.load(self._home, self._key).phase
            phase = getattr(phase, "value", phase)
            from rich.markup import escape
            model = steplog.session_model(cur)["model"]
            self._emit(gen, f"[dim]── next session ({escape(str(phase))}, {escape(model)}) ──[/dim]")
            self.app.call_from_thread(self._set_current, gen, cur)
            self._follow(gen, cur, claim.get("pid"), "confirmed")

    def _follow(self, gen: int, log_path: Path, live_pid, verdict: str) -> None:
        """Stream *log_path* until its writer is gone (dead pid / denied claim / no pid)."""
        is_stream = log_path.name.endswith(".stream.jsonl")
        is_opencode = log_path.name.endswith(".opencode.jsonl")
        is_pi = log_path.name.endswith(".pi.jsonl")
        render_line = (render_pi_log_line if is_pi
                       else render_opencode_log_line if is_opencode
                       else render_log_line)

        with log_path.open(encoding="utf-8", errors="replace") as f:
            buf = ""
            while self._live(gen):
                chunk = f.read(4096)
                if chunk:
                    buf += chunk
                    if is_stream or is_opencode or is_pi:
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            for rendered in render_line(obj):
                                self._emit(gen, rendered)
                    else:
                        self._emit(gen, chunk)
                else:
                    if verdict == "denied":
                        break
                    if live_pid and not claims.pid_alive(live_pid):
                        break
                    if not live_pid:
                        break
                    time.sleep(0.25)
        if self._live(gen):
            self.app.call_from_thread(self._update_header)


def _session_header(sess: dict, outcome: str, info: dict) -> str:
    """One-line per-session banner: id, start ts, format, outcome, runner, model (markup-escaped)."""
    from rich.markup import escape
    from .. import steplog
    return escape(steplog.format_session_header(sess, outcome, info))


class FleetScreen(Screen):
    """Full-screen fleet & health panel."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("u", "fleet_up", "Up"),
        ("d", "fleet_down", "Down"),
        ("s", "dispatch_sweep", "Sweep"),
        ("S", "dispatch_real", "Real sweep"),
        ("p", "project_rebuild", "Project"),
        ("P", "toggle_pause", "Pause/Resume"),
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
        # health.report() shells out to gh/launchctl/worktree probes and can run past
        # 10s; a shorter interval than that keeps cancelling it via exclusive=True
        # before it ever reaches SUCCESS, so #fleet-status never leaves "Loading…".
        self.set_interval(20.0, self._refresh_worker)

    # --- workers (run in threads so the event loop stays free) ---------------

    def _refresh_worker(self) -> None:
        self.run_worker(self._load_status, thread=True, group="refresh", exclusive=True,
                        name="fleet-refresh")

    def _load_status(self) -> tuple[dict, dict]:
        status = fleet_mod.status(self._home)
        now = store.now_epoch()
        doctor = health.report(config_mod.load(str(self._home)), now)
        doctor["rate_limit"] = ratelimit.status(self._home, now)
        return status, doctor

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.SUCCESS:
            if event.worker.name == "fleet-refresh":
                self._status, self._doctor = event.worker.result
                self.query_one("#fleet-status", Static).update(
                    _render_fleet(self._status, self._doctor)
                )
            elif event.worker.name in ("dispatch-sweep", "dispatch-sweep-real", "project-rebuild"):
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
                lambda: fleet_mod.up(self._home, interval=interval,
                                     cfg=config_mod.load(str(self._home))),
                thread=True, name="fleet-up",
            )
            self._log(f"fleet up --interval {interval} … ")
            self._refresh_worker()

        self.app.push_screen(_IntervalModal(), _on_interval)

    def action_fleet_down(self) -> None:
        def _on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            self.run_worker(lambda: fleet_mod.down(self._home), thread=True, name="fleet-down")
            self._log("fleet down … ")
            self._refresh_worker()

        self.app.push_screen(
            _ConfirmModal("Take the [bold]fleet down[/bold]? Dispatch stops until you bring it up."),
            _on_confirm,
        )

    def action_toggle_pause(self) -> None:
        paused = self._status.get("paused", False)
        if paused:
            self.run_worker(lambda: fleet_mod.resume(self._home), thread=True, name="fleet-resume")
            self._log("fleet resume … ")
            self._refresh_worker()
            return

        def _on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            self.run_worker(lambda: fleet_mod.pause(self._home), thread=True, name="fleet-pause")
            self._log("fleet pause … ")
            self._refresh_worker()

        self.app.push_screen(
            _ConfirmModal("[bold]Pause[/bold] the fleet? No new sessions spawn until resumed."),
            _on_confirm,
        )

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

    def action_dispatch_real(self) -> None:
        def _on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._log("dispatching (real sweep) … ")
            self.run_worker(self._run_dispatch_real, thread=True, name="dispatch-sweep-real")

        self.app.push_screen(
            _ConfirmModal("Run a [bold]real[/bold] dispatch sweep? This may mint and spawn sessions."),
            _on_confirm,
        )

    def _run_dispatch_real(self) -> str:
        try:
            p = subprocess.run(
                ["maestro", "--home", str(self._home), "dispatch"],
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


class DepsScreen(Screen):
    """Full-screen dependency tree of every open ticket, colored by blocking depth."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "refresh_deps", "Refresh"),
        ("enter", "open_detail", "Detail"),
        ("s", "open_spec", "Spec"),
    ]

    CSS = """
    DepsScreen #deps-header { padding: 0 2; height: auto; }
    DepsScreen #deps-tree   { height: 1fr; }
    """

    def __init__(self, home: Path, focus_key: str | None = None) -> None:
        super().__init__()
        self._home = home
        self._focus_key = focus_key
        self._graph: depgraph.DepGraph | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[dim]Loading…[/dim]", id="deps-header")
        yield Tree("dependencies", id="deps-tree")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Dependencies"
        self.query_one("#deps-tree", Tree).show_root = False
        self._refresh_worker()
        self.set_interval(10.0, self._refresh_worker)

    def _refresh_worker(self) -> None:
        self.run_worker(lambda: depgraph.build(self._home), thread=True, group="refresh",
                        exclusive=True, name="deps-refresh")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "deps-refresh":
            return
        if event.state == WorkerState.SUCCESS:
            self._graph = event.worker.result
            self._populate()
        elif event.state == WorkerState.ERROR:
            self.query_one("#deps-header", Static).update(
                f"[red]deps refresh failed: {event.worker.error}[/red]")

    def _populate(self) -> None:
        g = self._graph
        tree = self.query_one("#deps-tree", Tree)
        want = self._focus_key or self._current_key()
        tree.clear()
        palette = self.app.get_css_variables()
        self.query_one("#deps-header", Static).update(_render_dep_header(g, palette))
        shown: dict[str, object] = {}
        in_cycle = {k for c in g.cycles for k in c}

        def add(parent, key: str) -> None:
            if key in shown:
                parent.add_leaf(Text.from_markup(
                    f"[dim]{key} ↑ shown above[/dim]"), data=key)
                return
            node = parent.add(_dep_label(g, key, palette, key in in_cycle),
                              data=key, expand=True)
            shown[key] = node
            for child in g.dependents[key]:
                add(node, child)

        for root in g.roots:
            add(tree.root, root)
        target = shown.get(want) if want else None
        if target is not None:
            tree.call_after_refresh(tree.move_cursor, target)
            self._focus_key = None

    def _current_key(self) -> str | None:
        node = self.query_one("#deps-tree", Tree).cursor_node
        return node.data if node is not None else None

    def action_refresh_deps(self) -> None:
        self._refresh_worker()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter on a focused Tree emits NodeSelected and consumes the key, so the
        # screen-level `enter` binding never fires -- open the detail view here.
        self.action_open_detail()

    def action_open_detail(self) -> None:
        key = self._current_key()
        if key is not None:
            self.app.push_screen(DetailScreen(self._home, key))

    def action_open_spec(self) -> None:
        key = self._current_key()
        if key is not None:
            self.app.push_screen(SpecScreen(self._home, key))


class SpecScreen(Screen):
    """Full-screen spec viewer + pending inbox for one ticket."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("e", "edit_spec", "Edit"),
        ("r", "refresh_spec", "Refresh"),
    ]

    CSS = """
    SpecScreen #spec-body   { height: 1fr; }
    SpecScreen #spec-deps   { height: auto; max-height: 4;
                               border-top: solid $primary; padding: 0 1; }
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
        yield Static("", id="spec-deps", markup=False)
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
        deps = depgraph.dep_status(self._home, self._key)
        deps_w = self.query_one("#spec-deps", Static)
        deps_w.display = bool(deps)
        deps_w.update("Dependencies  " + " · ".join(
            f"{emoji} {dep} #{phase}" for dep, emoji, phase in deps))
        pending_markup = _render_pending(pending_cmds)
        self.query_one("#spec-pending", Static).update(
            f"[bold]Pending inbox[/bold]  {pending_markup}"
        )

    def action_edit_spec(self) -> None:
        spec_path = self._home / "tickets" / self._key / "spec.md"
        warning = edit_in_editor(self.app, spec_path)
        if warning:
            self.notify(warning, severity="warning")
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
        runner, runner_model = spec_runner(self._home, self._key)
        self.query_one("#ds-detail", Static).update(
            _render_detail(snap, snap_mod.display_title(self._home, snap),
                           runner, runner_model))
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
        table.add_column("Title")
        table.add_column("Repo")
        table.add_column("Cadence")
        table.add_column("Kind")
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
            cadence = f"{row['cron']} ({row['tz']})" if row.get("cron") else str(row["every"])
            table.add_row(
                row["name"], row.get("title") or "", row.get("repo") or "",
                cadence, row["kind"],
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
            try:
                added = ops.schedule_add(cfg, result)
            except store.MaestroError as e:
                self.notify(str(e), severity="warning")
                return
            self.notify(f"Added scheduled task {added['name']!r}")
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
            # ops.schedule_edit merges `result` onto the existing task itself --
            # the modal only renders a subset of a task's fields, so a wholesale
            # swap would silently drop anything the modal doesn't know about
            # (e.g. model/effort/notes/depends_on set by hand).
            cfg2 = config_mod.load(str(self._home))
            try:
                updated = ops.schedule_edit(cfg2, self._selected_name, result)
            except store.MaestroError as e:
                self.notify(str(e), severity="warning")
                return
            self.notify(f"Updated scheduled task {updated['name']!r}")
            self._refresh()

        self.app.push_screen(_ScheduleModal(existing), _on_dismiss)

    def action_toggle_task(self) -> None:
        if self._selected_name is None:
            self.notify("Select a task first", severity="warning")
            return
        cfg = config_mod.load(str(self._home))
        existing = next((t for t in self._tasks(cfg) if t.get("name") == self._selected_name), None)
        if existing is None:
            self.notify("Task not found (config may have changed)", severity="warning")
            return
        try:
            ops.schedule_set_enabled(cfg, self._selected_name, not existing.get("enabled", True))
        except store.MaestroError as e:
            self.notify(str(e), severity="warning")
            return
        self.notify(f"Toggled {self._selected_name}")
        self._refresh()
