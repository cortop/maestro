"""Detail-pane rendering — no textual dependency, importable in tests."""
from __future__ import annotations

from . import snapshot as snap_mod

_EM = "—"  # em-dash for missing values


def render(snap: snap_mod.Snapshot) -> str:
    """Build Rich markup string for the snapshot detail pane."""
    def v(val: object) -> str:
        return str(val) if val is not None and val != "" else _EM

    pr_info = _EM
    if snap.pr_url and snap.pr_number:
        draft = " [dim](draft)[/dim]" if snap.pr_draft else ""
        pr_info = f"[link={snap.pr_url}]#{snap.pr_number}[/link]{draft} ({v(snap.pr_state)})"

    questions = _EM
    if snap.open_questions:
        questions = "\n  ".join(
            f"[yellow]{qid}[/yellow]: {text}"
            for qid, text in snap.open_questions.items()
        )

    return (
        f"[bold]{v(snap.title)}[/bold]\n\n"
        f"[dim]Key[/dim]           {v(snap.key)}\n"
        f"[dim]Phase[/dim]         {v(snap.phase)}\n"
        f"[dim]Tier[/dim]          {v(snap.tier)}\n"
        f"[dim]Source[/dim]        {v(snap.source)}\n"
        f"[dim]PR[/dim]            {pr_info}\n"
        f"[dim]CI[/dim]            {v(snap.ci_state)}\n"
        f"[dim]Failures[/dim]      {snap.failure_count}\n"
        f"[dim]Last error[/dim]    {v(snap.last_error)}\n"
        f"[dim]Open questions[/dim] {questions}\n"
        f"[dim]Updated[/dim]       {v(snap.updated_ts)}\n"
    )
