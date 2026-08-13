"""Detail-pane rendering — no textual dependency, importable in tests."""
from __future__ import annotations

from .. import ops as ops_mod
from .. import snapshot as snap_mod

_EM = "—"  # em-dash for missing values


def _esc(s: str) -> str:
    """Escape dynamic values so a literal '[' in user/agent data (e.g. a bracketed
    error message) can't be mis-parsed as a Textual markup tag and crash the pane."""
    return s.replace("\\", "\\\\").replace("[", "\\[")


def render(snap: snap_mod.Snapshot, tier: int | None = None,
           title: str | None = None, runner: str | None = None,
           runner_model: str | None = None) -> str:
    """Build Rich markup string for the snapshot detail pane.

    `tier` comes from the caller (`dispatcher.spec_tier`) -- the snapshot itself
    carries no tier field, so display and the approval gate read the same source
    by construction. `tier` is total once read from disk and never None in
    practice, but 0 is a real (auto-approved) tier, so it's rendered via
    `str(tier)` unconditionally rather than the `v()` falsy-check helper below.

    `title` arrives the same way (`projection.display_title`) and for the same
    reason: a ticket whose log carries no `TicketCreated` folds to `title = None`
    but still has its title in its spec's H1, and this module stays filesystem-
    free so it remains importable in tests. Omitted, it falls back to the folded
    `snap.title`, so every existing caller renders exactly as before.

    `runner`/`runner_model` (UX-2) come the same way, from `dispatcher.spec_runner`
    -- the snapshot carries no runner field either, and this module stays
    filesystem-free. A `None` runner means "no spec override", which renders as
    the board default (`"claude"`), not an em-dash -- an absent override is a
    normal, common state, not a missing value.
    """
    def v(val: object) -> str:
        return _esc(str(val)) if val is not None and val != "" else _EM

    pr_info = _EM
    if snap.pr_url and snap.pr_number:
        draft = " [dim](draft)[/dim]" if snap.pr_draft else ""
        pr_info = f'[link="{snap.pr_url}"]#{snap.pr_number}[/link]{draft} ({v(snap.pr_state)})'

    questions = _EM
    if snap.open_questions:
        lines = []
        for qid, text in snap.open_questions.items():
            position, total, body, recommend = ops_mod.parse_round_question(text)
            head = f"[yellow]{_esc(qid)}[/yellow]"
            if position and total:
                head += f" [dim]({position}/{total})[/dim]"
            line = f"{head}: {_esc(body)}"
            if recommend:
                line += f"\n    [dim]Recommended:[/dim] {_esc(recommend)}"
            lines.append(line)
        questions = "\n  ".join(lines)

    runner_info = _esc(runner if runner else "claude")
    if runner_model:
        runner_info += f" [dim]({_esc(runner_model)})[/dim]"

    return (
        f"[bold]{v(title if title is not None else snap.title)}[/bold]\n\n"
        f"[dim]Key[/dim]           {v(snap.key)}\n"
        f"[dim]Phase[/dim]         {v(snap.phase)}\n"
        f"[dim]Tier[/dim]          {str(tier) if tier is not None else _EM}\n"
        f"[dim]Runner[/dim]        {runner_info}\n"
        f"[dim]Source[/dim]        {v(snap.source)}\n"
        f"[dim]PR[/dim]            {pr_info}\n"
        f"[dim]CI[/dim]            {v(snap.ci_state)}\n"
        f"[dim]Failures[/dim]      {snap.failure_count}\n"
        f"[dim]Last error[/dim]    {v(snap.last_error)}\n"
        f"[dim]Open questions[/dim] {questions}\n"
        f"[dim]Updated[/dim]       {v(snap.updated_ts)}\n"
    )


def render_pending(cmds: list[dict]) -> str:
    """Build Rich markup string listing unconsumed inbox commands."""
    if not cmds:
        return _EM
    lines = []
    for cmd in cmds:
        ts = _esc(str(cmd.get("ts", "")))
        command = _esc(str(cmd.get("command", "")))
        args = cmd.get("args", {})
        args_str = "  ".join(
            f"[dim]{_esc(k)}[/dim]={_esc(str(v))}" for k, v in args.items()
        )
        line = f"[dim]{ts}[/dim]  [bold cyan]{command}[/bold cyan]"
        if args_str:
            line += f"  {args_str}"
        lines.append(line)
    return "\n".join(lines)
