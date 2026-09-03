"""Modal input dialogs (answer, command palette, create, inbox, confirm, schedule)."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from .. import schedule, store
from ..providers import ollama as ollama_mod
from ..providers import pi as pi_mod
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
        ("ctrl+s", "submit", "Submit"),
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
    #answer-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
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
            yield TextArea(id="answer-input")
            with Horizontal(id="answer-buttons"):
                yield Button("Submit", id="answer-submit-button", variant="primary")
            yield Label("[dim]Enter → newline · Ctrl+S or button → submit · Esc → cancel[/dim]")

    def on_mount(self) -> None:
        self.query_one("#answer-input", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "answer-submit-button":
            self.action_submit()

    def action_submit(self) -> None:
        text = self.query_one("#answer-input", TextArea).text.strip()
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


def _commands_for(phase: str) -> list[tuple[str, str]]:
    """The reference list `_CmdModal` shows for *phase*."""
    return _PHASE_COMMANDS.get(phase, _DEFAULT_COMMANDS)


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
        commands = _commands_for(self._phase)
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


class _ImportLinearModal(ModalScreen):
    """T-103: prompt for a Linear issue URL or bare identifier; dismisses with
    the raw string (or None on cancel/blank). Parsing/minting itself happens
    in `ops.import_linear`, called by the app after dismissal -- this modal
    is just the input surface."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="answer-dialog"):
            yield Label("[bold]Import from Linear[/bold] — paste an issue URL or identifier")
            yield Input(placeholder="https://linear.app/<org>/issue/ENG-123/... or ENG-123",
                        id="import-linear-input")

    def on_mount(self) -> None:
        self.query_one("#import-linear-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        self.dismiss(raw or None)

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
        priority_str = self.query_one("#create-priority", Input).value.strip() or "3"
        intent_val = self.query_one("#create-intent", TextArea).text.strip() or None
        try:
            priority = int(priority_str)
        except ValueError:
            self.notify("Priority must be an integer", severity="warning")
            return
        kind_sel = self.query_one("#create-kind", Select)
        kind_val = str(kind_sel.value) if kind_sel.value is not Select.BLANK else "implementation"
        model_val = self.query_one("#create-model", Input).value.strip() or None
        effort_val = self.query_one("#create-effort", Input).value.strip() or None
        self.dismiss({
            "title": title,
            "prefix": prefix,
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
            yield Label("Title (optional; falls back to name)")
            yield Input(value=t.get("title") or "", placeholder="e.g. Morning PR digest", id="sched-title")
            yield Label("Prompt [bold red]*[/bold red]")
            yield TextArea(t.get("prompt", ""), id="sched-prompt")
            yield Label("Every (e.g. 30m / 6h / 24h / seconds) -- exactly one of Every/Cron")
            yield Input(value=str(t.get("every") or ""), placeholder="30m", id="sched-every")
            yield Label("Cron (5-field, e.g. '0 9 * * 1') -- exactly one of Every/Cron")
            yield Input(value=t.get("cron") or "", placeholder="0 9 * * 1", id="sched-cron")
            yield Label("Timezone (IANA, e.g. America/New_York; default UTC; only used by Cron)")
            yield Input(value=t.get("tz") or "", placeholder="UTC", id="sched-tz")
            yield Label("Repo (optional; must match a [repos.<name>] table)")
            yield Input(value=t.get("repo") or "", placeholder="e.g. alpha", id="sched-repo")
            yield Label("Kind")
            yield Select(options=[("implementation", "implementation"), ("research", "research")],
                        id="sched-kind", allow_blank=False, value=t.get("kind", "implementation"))
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
        cron = self.query_one("#sched-cron", Input).value.strip()
        if bool(every) == bool(cron):
            self.notify("Exactly one of Every or Cron is required", severity="warning")
            return
        if every:
            try:
                schedule.parse_every(every)
            except ValueError:
                self.notify("Every must look like 30m / 6h / 24h / seconds", severity="warning")
                return
        else:
            try:
                schedule.parse_cron(cron)
            except ValueError as e:
                self.notify(f"Cron: {e}", severity="warning")
                return
        tz = self.query_one("#sched-tz", Input).value.strip() or None
        if tz:
            try:
                schedule.resolve_tz(tz)
            except ValueError as e:
                self.notify(str(e), severity="warning")
                return
        try:
            priority = int(self.query_one("#sched-priority", Input).value.strip() or "3")
        except ValueError:
            self.notify("Priority must be an integer", severity="warning")
            return
        kind_sel = self.query_one("#sched-kind", Select)
        kind = str(kind_sel.value) if kind_sel.value is not Select.BLANK else "implementation"
        prefix = self.query_one("#sched-prefix", Input).value.strip() or None
        enabled_sel = self.query_one("#sched-enabled", Select)
        enabled = str(enabled_sel.value) == "true"
        title = self.query_one("#sched-title", Input).value.strip() or None
        repo = self.query_one("#sched-repo", Input).value.strip() or None
        self.dismiss({
            "name": name, "prompt": prompt, "every": every or None, "cron": cron or None,
            "tz": tz, "kind": kind,
            "priority": priority, "prefix": prefix,
            "enabled": enabled, "title": title, "repo": repo,
        })


class _RunnerModal(ModalScreen):
    """Edit one ticket's `runner:`/`runner_model:` spec front-matter (UX-2);
    dismisses with ``{"runner", "runner_model"}`` or ``None`` on cancel. Copies
    `_ScheduleModal`'s "edit an existing record" shape and
    `_CreateModal.on_select_changed`'s kind -> field reshape. All state
    mutation happens in the caller's `_on_dismiss` (via `ops.set_runner`) --
    this modal only collects the two values, it never touches the spec file
    itself.

    T-61 (PI-9): three runner kinds now, each with its own model catalogue --
    claude has none of its own (the field stays sourced from ollama's for a
    claude-runner ticket too, a pre-existing quirk this ticket doesn't
    change: the label already says "only meaningful for a non-claude
    runner"), opencode's is `providers.ollama`, pi's is `providers.pi`.
    Selecting a runner-kind reshapes `#runner-model` to that kind's own
    catalogue (`_reshape_model_field`) -- a `Select` when the catalogue is
    reachable, an `Input` fallback otherwise, same shape either kind uses."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+enter", "submit", "Submit"),
    ]

    # UX-1's known runner names (`maestro runners`) -- "claude" is always
    # available; anything else is opt-in via the board's `runner_enabled`.
    _RUNNER_OPTIONS = [("claude", "claude"), ("opencode", "opencode"), ("pi", "pi")]

    def __init__(self, key: str, runner: str | None, runner_model: str | None,
                home: Path | None = None) -> None:
        super().__init__()
        self._key = key
        self._runner = runner or "claude"
        self._runner_model = runner_model or ""
        # `home` resolves pi's own agent dir (`store.pi_agent_dir`) for its
        # catalogue probe -- optional so a caller that never shows a pi
        # ticket (or a test exercising claude/opencode only) doesn't need one.
        self._home = home
        # UX-1's model catalogue (`ollama_mod.fetch_models`) -- fetched once,
        # up front, so both `compose` and `on_select_changed` decide the model
        # field's shape off the same result. Never raises (see ollama.py); a
        # down/unreachable daemon just means an empty catalogue, handled below.
        self._models, self._daemon_reason = ollama_mod.fetch_models()
        # pi's own catalogue is fetched lazily (`_fetch_pi_models`) -- a
        # subprocess probe, unlike ollama's cheap 2s-timeout HTTP call, so it
        # must never run for a modal that never shows a pi-kind selection.
        self._pi_models: list[dict] | None = None
        self._pi_reason: str | None = None
        self._pi_fetched = False

    def _fetch_pi_models(self) -> None:
        if self._pi_fetched:
            return
        pi_agent_dir = store.pi_agent_dir(self._home) if self._home is not None else None
        self._pi_models, self._pi_reason = pi_mod.fetch_models(pi_agent_dir)
        self._pi_fetched = True

    def _catalogue(self, runner: str) -> tuple[list[dict] | None, str | None, list[str]]:
        """``(models, reason, names)`` for *runner*'s own model field -- pi's
        catalogue for a pi selection, ollama's for anything else."""
        if runner == "pi":
            self._fetch_pi_models()
            models, reason = self._pi_models, self._pi_reason
            names = pi_mod.model_names(models) if models else []
        else:
            models, reason = self._models, self._daemon_reason
            names = ollama_mod.model_names(models, tool_capable_only=True) if models else []
        return models, reason, names

    def _model_field_widget(self, runner: str, current: str | None) -> Input | Select:
        models, _reason, names = self._catalogue(runner)
        if models is not None:
            model_options = [(m, m) for m in names]
            default = current if current in dict(model_options) else Select.NULL
            return Select(options=model_options, id="runner-model", allow_blank=True, value=default)
        return Input(value=current or "",
                    placeholder="model name (daemon unreachable)", id="runner-model")

    def compose(self) -> ComposeResult:
        with Vertical(id="runner-dialog"):
            yield Label(f"[bold]{self._key}[/bold] — runner")
            yield Label("Runner")
            yield Select(options=self._RUNNER_OPTIONS, id="runner-kind",
                        allow_blank=False, value=self._runner)
            yield Label("Model (optional; only meaningful for a non-claude runner)")
            yield self._model_field_widget(self._runner, self._runner_model or None)
            yield Label("[dim]Tab/Enter → next · Ctrl+Enter → submit · Esc → cancel[/dim]", id="runner-hint")

    def on_mount(self) -> None:
        models, reason, _names = self._catalogue(self._runner)
        if models is None:
            source = "pi" if self._runner == "pi" else "ollama daemon"
            self.notify(
                f"{source} unreachable ({reason}) -- "
                "type a model name manually", severity="warning",
            )
        self.query_one("#runner-kind", Select).focus()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "runner-kind":
            return
        await self._reshape_model_field(str(event.value))

    async def _reshape_model_field(self, runner: str) -> None:
        """Rebuild `#runner-model` for *runner*'s own catalogue. A
        runner_model override is only ever meaningful for a non-claude
        runner (`ops.set_runner` only validates/warns on it then), so
        `claude` just clears whatever the field holds -- same as before T-61.
        `opencode`/`pi` reshape the field to that kind's own catalogue
        (`_catalogue`), carrying over the current value where it's still a
        valid choice under the new one.

        Async (Textual awaits a coroutine message handler same as a plain
        one): swapping the field's WIDGET TYPE (`Select` <-> `Input`, needed
        whenever reachability differs between the two kinds) must `remove()`
        the old widget and `await` that completion before `mount()`-ing the
        new one under the same id -- `mount()` registers the new widget's id
        synchronously, while `remove()` only schedules the old one's
        deregistration, so an un-awaited remove immediately followed by a
        mount raises `DuplicateIds`."""
        old = self.query_one("#runner-model")
        if isinstance(old, Select):
            current = None if old.value is Select.NULL else str(old.value)
        else:
            current = old.value.strip() or None
        if runner == "claude":
            if isinstance(old, Select):
                old.value = Select.NULL
            else:
                old.value = ""
            return
        models, reason, names = self._catalogue(runner)
        if models is None:
            source = "pi" if runner == "pi" else "ollama daemon"
            self.notify(f"{source} unreachable ({reason}) -- type a model name manually",
                       severity="warning")
        if isinstance(old, Select) and models is not None:
            model_options = [(m, m) for m in names]
            old.set_options(model_options)
            old.value = current if current in dict(model_options) else Select.NULL
            return
        if isinstance(old, Input) and models is None:
            return  # already the right shape -- keep whatever's typed
        new_widget = self._model_field_widget(runner, current)
        hint = self.query_one("#runner-hint")
        await old.remove()
        await self.query_one("#runner-dialog").mount(new_widget, before=hint)

    def action_submit(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        # runner-kind has allow_blank=False, so .value is always a real option.
        runner = str(self.query_one("#runner-kind", Select).value)
        model_widget = self.query_one("#runner-model")
        if isinstance(model_widget, Select):
            runner_model = None if model_widget.value is Select.NULL else str(model_widget.value)
        else:
            runner_model = model_widget.value.strip() or None
        self.dismiss({"runner": runner, "runner_model": runner_model})
