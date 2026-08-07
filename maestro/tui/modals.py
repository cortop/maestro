"""Modal input dialogs (answer, command palette, create, inbox, confirm, schedule)."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Select, Static, TextArea

from .. import schedule
from ..statemachine import Phase


# Sentinel dismissal value meaning "accept the shown recommendation for every
# remaining question in the round that carries one" -- distinct from a normal
# typed-answer string (which is always non-empty, see on_input_submitted) or
# None (cancel), so the caller (`MaestroTUI._walk_questions`) can tell the three
# apart without a wrapper type.
_ACCEPT_ALL = object()


class _AnswerModal(ModalScreen):
    """Single-question input modal; dismisses with the answer string, `_ACCEPT_ALL`,
    or None on cancel."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+r", "accept_recommendation", "Accept recommendation"),
        ("ctrl+g", "accept_all_remaining", "Accept all remaining"),
    ]

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
    #recommend-scroll {
        max-height: 4;
        margin-bottom: 1;
    }
    """

    def __init__(self, key: str, qid: str, position: int | None, total: int | None,
                 question_text: str, recommend: str | None, remaining: int, home: Path) -> None:
        super().__init__()
        self._key = key
        self._qid = qid
        self._position = position
        self._total = total
        self._question_text = question_text
        self._recommend = recommend
        self._remaining = remaining
        self._home = home

    def compose(self) -> ComposeResult:
        header = f"[bold]{self._key}[/bold]"
        if self._position and self._total:
            header += f" — {self._position} of {self._total}"
        header += f" ({self._remaining} remaining)"
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
            if self._recommend:
                yield Label("[dim]── Recommended ──[/dim]")
                with VerticalScroll(id="recommend-scroll"):
                    yield Static(self._recommend, markup=False)
                yield Label(
                    "[dim]Ctrl+R accept this recommendation · "
                    "Ctrl+G accept all remaining recommendations[/dim]"
                )
            yield Input(placeholder="Answer (Enter to submit, Esc to cancel)", id="answer-input")

    def on_mount(self) -> None:
        self.query_one("#answer-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_accept_recommendation(self) -> None:
        if not self._recommend:
            self.notify("No recommendation for this question", severity="warning")
            return
        self.dismiss(self._recommend)

    def action_accept_all_remaining(self) -> None:
        self.dismiss(_ACCEPT_ALL)


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

    _NEW_PREFIX = "(new)"

    def __init__(self, existing_prefixes: list[str] | None = None) -> None:
        super().__init__()
        self._prefixes = existing_prefixes or []

    def compose(self) -> ComposeResult:
        options = [(p, p) for p in self._prefixes] + [(self._NEW_PREFIX, self._NEW_PREFIX)]
        default_prefix = "M" if "M" in self._prefixes else Select.NULL
        with Vertical(id="create-dialog"):
            yield Label("[bold]New Ticket[/bold]")
            yield Label("Title [bold red]*[/bold red]")
            yield Input(placeholder="required", id="create-title")
            yield Label("Prefix [bold red]*[/bold red]")
            yield Select(options=options, id="create-prefix", allow_blank=False, value=default_prefix)
            yield Input(placeholder="new prefix, e.g. FEAT", id="create-prefix-new")
            yield Label("Kind")
            yield Select(options=[("implementation", "implementation"), ("research", "research")], id="create-kind", allow_blank=False, value="implementation")
            yield Label("Model")
            yield Input(placeholder="e.g. opus, sonnet (empty = config default)", id="create-model")
            yield Label("Effort")
            yield Input(placeholder="e.g. high, medium, low (empty = config default)", id="create-effort")
            yield Label("Tier")
            yield Input(value="1", id="create-tier")
            yield Label("Priority")
            yield Input(value="3", id="create-priority")
            yield Label("Intent")
            yield TextArea(id="create-intent")
            yield Label("[dim]Tab/Enter → next · Ctrl+Enter → submit · Esc → cancel[/dim]")

    def on_mount(self) -> None:
        self.query_one("#create-title", Input).focus()
        # show new-prefix input only when (new) is the sole/initial selection
        self.query_one("#create-prefix-new", Input).display = not bool(self._prefixes)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "create-prefix":
            is_new = (event.value == self._NEW_PREFIX)
            new_inp = self.query_one("#create-prefix-new", Input)
            new_inp.display = is_new
            if is_new:
                new_inp.focus()
        elif event.select.id == "create-kind":
            model_inp = self.query_one("#create-model", Input)
            effort_inp = self.query_one("#create-effort", Input)
            if event.value == "research":
                model_inp.value = "opus"
                effort_inp.value = "high"
            else:
                model_inp.value = ""
                effort_inp.value = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        visible = [w for w in self.query(Input) if w.display]
        if event.input not in visible:
            return
        idx = visible.index(event.input)
        if idx < len(visible) - 1:
            visible[idx + 1].focus()
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
        select = self.query_one("#create-prefix", Select)
        selected = select.value
        if selected is Select.BLANK:
            self.notify("Select a prefix", severity="warning")
            return
        if selected == self._NEW_PREFIX:
            prefix = self.query_one("#create-prefix-new", Input).value.strip().upper()
            if not prefix:
                self.notify("Prefix name is required", severity="warning")
                self.query_one("#create-prefix-new", Input).focus()
                return
        else:
            prefix = str(selected)
        tier_str = self.query_one("#create-tier", Input).value.strip() or "1"
        priority_str = self.query_one("#create-priority", Input).value.strip() or "3"
        intent_val = self.query_one("#create-intent", TextArea).text.strip() or None
        try:
            tier = int(tier_str)
            priority = int(priority_str)
        except ValueError:
            self.notify("Tier and priority must be integers", severity="warning")
            return
        kind_sel = self.query_one("#create-kind", Select)
        kind_val = str(kind_sel.value) if kind_sel.value is not Select.BLANK else "implementation"
        model_val = self.query_one("#create-model", Input).value.strip() or None
        effort_val = self.query_one("#create-effort", Input).value.strip() or None
        self.dismiss({
            "title": title,
            "prefix": prefix,
            "tier": tier,
            "priority": priority,
            "intent": intent_val,
            "kind": kind_val,
            "model": model_val,
            "effort": effort_val,
        })


class _InboxModal(ModalScreen):
    """Free-form inbox message compose modal; dismisses with the message string or None on cancel."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    _InboxModal {
        align: center middle;
    }
    #inbox-dialog {
        width: 70%;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def compose(self) -> ComposeResult:
        with Vertical(id="inbox-dialog"):
            yield Label(f"[bold]{self._key}[/bold] — send to inbox")
            yield Label("[dim]Message will be queued for the next reconciler sweep.[/dim]")
            yield Input(placeholder="Message (Enter to send, Esc to cancel)", id="inbox-input")

    def on_mount(self) -> None:
        self.query_one("#inbox-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


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


class _ScheduleModal(ModalScreen):
    """Add/edit form for one `[[scheduled]]` task; dismisses with a result dict or None."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+enter", "submit", "Submit"),
    ]

    def __init__(self, existing: dict | None = None) -> None:
        super().__init__()
        self._existing = existing or {}

    def compose(self) -> ComposeResult:
        t = self._existing
        with Vertical(id="schedule-dialog"):
            yield Label("[bold]Scheduled task[/bold]")
            yield Label("Name [bold red]*[/bold red]")
            yield Input(value=t.get("name", ""), placeholder="required, stable id", id="sched-name")
            yield Label("Prompt [bold red]*[/bold red]")
            yield TextArea(t.get("prompt", ""), id="sched-prompt")
            yield Label("Every (e.g. 30m / 6h / 24h / seconds) [bold red]*[/bold red]")
            yield Input(value=str(t.get("every", "")), placeholder="30m", id="sched-every")
            yield Label("Kind")
            yield Select(options=[("implementation", "implementation"), ("research", "research")],
                        id="sched-kind", allow_blank=False, value=t.get("kind", "implementation"))
            yield Label("Approval tier")
            yield Input(value=str(t.get("approval_tier", 1)), id="sched-tier")
            yield Label("Priority")
            yield Input(value=str(t.get("priority", 3)), id="sched-priority")
            yield Label("Prefix (minted keys become PREFIX-1, PREFIX-2, …)")
            yield Input(value=t.get("prefix") or "", placeholder="e.g. S", id="sched-prefix")
            yield Label("Enabled")
            yield Select(options=[("true", "true"), ("false", "false")],
                        id="sched-enabled", allow_blank=False,
                        value="true" if t.get("enabled", True) else "false")
            yield Label("[dim]Tab/Enter → next · Ctrl+Enter → submit · Esc → cancel[/dim]")

    def on_mount(self) -> None:
        self.query_one("#sched-name", Input).focus()

    def action_submit(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        name = self.query_one("#sched-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="warning")
            self.query_one("#sched-name", Input).focus()
            return
        prompt = self.query_one("#sched-prompt", TextArea).text.strip()
        if not prompt:
            self.notify("Prompt is required", severity="warning")
            return
        every = self.query_one("#sched-every", Input).value.strip()
        if not every:
            self.notify("Every is required", severity="warning")
            return
        try:
            schedule.parse_every(every)
        except ValueError:
            self.notify("Every must look like 30m / 6h / 24h / seconds", severity="warning")
            return
        try:
            tier = int(self.query_one("#sched-tier", Input).value.strip() or "1")
            priority = int(self.query_one("#sched-priority", Input).value.strip() or "3")
        except ValueError:
            self.notify("Tier and priority must be integers", severity="warning")
            return
        kind_sel = self.query_one("#sched-kind", Select)
        kind = str(kind_sel.value) if kind_sel.value is not Select.BLANK else "implementation"
        prefix = self.query_one("#sched-prefix", Input).value.strip() or None
        enabled_sel = self.query_one("#sched-enabled", Select)
        enabled = str(enabled_sel.value) == "true"
        self.dismiss({
            "name": name, "prompt": prompt, "every": every, "kind": kind,
            "approval_tier": tier, "priority": priority, "prefix": prefix,
            "enabled": enabled,
        })
