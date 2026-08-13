"""Interactive TUI for maestro — requires the `tui` extra (textual).

Package layout: `app.py` (MaestroTUI + entrypoint), `screens.py` (full-screen
views), `modals.py` (input dialogs), `render.py` (pure markup helpers),
`detail.py` / `events.py` (textual-free renderers). Everything external —
cli.py and the tests — imports through this namespace, so internal layout can
change without breaking importers.
"""
from .app import MaestroTUI, main, _FILTERS, _NEEDS_YOU_PHASES
from .modals import (
    _ACCEPT_ALL,
    _AnswerModal,
    _CmdModal,
    _ConfirmModal,
    _CreateModal,
    _DEFAULT_COMMANDS,
    _InboxModal,
    _IntervalModal,
    _PHASE_COMMANDS,
    _RunnerModal,
    _ScheduleModal,
)
from .render import (
    _PHASE_STYLE,
    _fmt_age,
    _fmt_epoch,
    _render_badge,
    _render_env,
    _render_fleet,
    _styled_row,
)
from .screens import (
    DetailScreen,
    EnvScreen,
    EventsScreen,
    FleetScreen,
    LogsScreen,
    ProposalScreen,
    ScheduleScreen,
    SpecScreen,
)

__all__ = [
    "MaestroTUI",
    "main",
    "DetailScreen",
    "EnvScreen",
    "EventsScreen",
    "FleetScreen",
    "LogsScreen",
    "ProposalScreen",
    "ScheduleScreen",
    "SpecScreen",
    "_ACCEPT_ALL",
    "_AnswerModal",
    "_CmdModal",
    "_ConfirmModal",
    "_CreateModal",
    "_InboxModal",
    "_IntervalModal",
    "_RunnerModal",
    "_ScheduleModal",
    "_DEFAULT_COMMANDS",
    "_PHASE_COMMANDS",
    "_FILTERS",
    "_NEEDS_YOU_PHASES",
    "_PHASE_STYLE",
    "_fmt_age",
    "_fmt_epoch",
    "_render_badge",
    "_render_env",
    "_render_fleet",
    "_styled_row",
]
