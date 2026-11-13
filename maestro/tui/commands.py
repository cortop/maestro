"""Command-palette providers: fuzzy ticket jump and a searchable list of every screen action (T-162)."""
from __future__ import annotations

from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from rich.text import Text

# Never offered: the palette must not list itself, and quit is one keystroke away.
_EXCLUDED_ACTIONS = frozenset({"quit", "command_palette", "app.quit", "app.command_palette"})
_EXACT_KEY, _PREFIX_KEY = 1e6, 1e3
_SUB_SCREENS = (("Detail", "DetailScreen"), ("Spec", "SpecScreen"), ("Logs", "LogsScreen"),
                ("Events", "EventsScreen"), ("Proposal", "ProposalScreen"))

# One-line help for each action that targets the selected ticket (keyed by action name).
_TICKET_ACTION_HELP = {
    "ac_matrix": "Acceptance criteria and their QA verdicts",
    "action_menu": "Pick any action for this ticket",
    "add_ac": "Append an acceptance criterion to the spec",
    "answer": "Answer the open question",
    "blockers": "What is blocking this ticket",
    "cmd": "Send a command to the ticket",
    "compact": "Compact the ticket's event log",
    "deps_panel": "Dependency tree around this ticket",
    "discard": "Discard a degraded ticket",
    "edit_spec": "Edit the spec in your editor",
    "focus_detail": "Open the ticket detail",
    "hold": "Hold the ticket so no session is spawned",
    "inbox_message": "Leave a message in the ticket's inbox",
    "now_accept": "Accept the recommended answer",
    "open_pr": "Open the ticket's pull request",
    "open_worktree": "Open the ticket's worktree",
    "release": "Release a held ticket",
    "retry": "Retry a degraded ticket",
    "rounds_panel": "Implementation rounds so far",
    "runner": "Choose the runner for this ticket",
    "show_spec": "Read the spec",
    "suggest_acs": "Draft acceptance criteria",
    "trigger_post_qa": "Run the post-QA step",
    "view_events": "The ticket's event log",
    "view_inbox": "The ticket's inbox log",
    "view_logs": "Session logs",
    "why_panel": "Why the ticket is in its current state",
}


class TicketProvider(Provider):
    """Fuzzy-match tickets by key and title (from the app's in-memory cache) and jump to them."""

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        q = query.strip().lower()
        for key, title, phase in list(app._palette_tickets):
            main = f"{key} {title}"
            score = matcher.match(main)
            # Matcher scores are unbounded (not 0..1): lift exact / prefix key matches clear of them.
            if q and key.lower() == q:
                score = _EXACT_KEY
            elif q and key.lower().startswith(q) and score > 0:
                score += _PREFIX_KEY
            if score > 0:
                yield Hit(score, matcher.highlight(main), lambda k=key: app._jump_to(k),
                          text=main, help=phase)
            for label, screen_name in _SUB_SCREENS:
                if screen_name == "ProposalScreen" and key not in app._palette_proposals:
                    continue
                sub = f"{key} › {label}"
                sub_score = matcher.match(sub) * 0.5
                if sub_score > 0:
                    yield Hit(min(sub_score, score * 0.99) if score > 0 else sub_score, matcher.highlight(sub),
                              lambda k=key, n=screen_name: app._open_ticket_screen(k, n),
                              text=sub, help=title)


class ActionProvider(Provider):
    """List the app's and the current screen's bindings as runnable "Description (key)" actions."""

    def _entries(self) -> list[tuple[Binding, bool | None, bool]]:
        """(binding, check_action result, is_screen_binding) for every offerable action."""
        app, screen = self.app, self.screen
        own = list(Binding.make_bindings(getattr(type(screen), "BINDINGS", None) or []))
        out: list[tuple[Binding, bool | None, bool]] = []
        shadowed = {b.key for b in own}
        seen: set[tuple[str, str]] = set()
        for binding, is_screen in [(b, True) for b in own] + [
                (b, False) for b in Binding.make_bindings(type(app).BINDINGS)]:
            if binding.action in _EXCLUDED_ACTIONS:
                continue
            if not is_screen and binding.key in shadowed:
                continue  # the screen's own key wins; show it only on that entry
            if (binding.key, binding.action) in seen:
                continue
            seen.add((binding.key, binding.action))
            name = binding.action.split("(", 1)[0]
            if name.startswith("app."):
                ok = app.check_action(name[4:], ())
            elif is_screen:
                ok = screen.check_action(name, ())
            else:
                ok = app.check_action(name, ())
            if ok is False:
                continue
            out.append((binding, ok, is_screen))
        return out

    def _label(self, binding: Binding) -> str:
        key = self.app.get_key_display(binding)
        return f"{binding.description or binding.action} ({key})"

    def _runner(self, binding: Binding, ok: bool | None, is_screen: bool):
        if ok is None:
            return lambda: self.app.notify(f"{binding.description or binding.action}: not available right now",
                                           severity="warning")
        target = self.screen if is_screen else self.app
        return lambda: target.run_action(binding.action)

    def _text(self, binding: Binding, ok: bool | None, label: str):
        return Text(label, style="dim") if ok is None else label

    async def discover(self) -> Hits:
        entries = self._entries()
        key = self.app._target_key()
        first = [e for e in entries if key and e[0].action.split("(", 1)[0] in self.app._TICKET_ACTIONS]
        for binding, ok, is_screen in first:  # the selected ticket's actions lead
            label = self._label(binding)
            name = binding.action.split("(", 1)[0]
            help_ = _TICKET_ACTION_HELP.get(name) or binding.description or name
            yield DiscoveryHit(self._text(binding, ok, label), self._runner(binding, ok, is_screen),
                               text=label, help=f"{key}: {help_}")
        rest = [e for e in entries if e not in first]
        for binding, ok, is_screen in sorted(rest, key=lambda e: e[0].show):  # stable: hidden first
            label = self._label(binding)
            yield DiscoveryHit(self._text(binding, ok, label), self._runner(binding, ok, is_screen),
                               text=label, help=binding.tooltip or None)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for binding, ok, is_screen in self._entries():
            label = self._label(binding)
            score = matcher.match(label)
            if score > 0:
                shown = matcher.highlight(label)
                if ok is None:
                    shown.stylize("dim")
                yield Hit(score, shown, self._runner(binding, ok, is_screen), text=label)
