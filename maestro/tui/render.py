"""Pure Rich-markup render/format helpers shared by the app and screens."""
from __future__ import annotations

import time

from rich.text import Text

from .. import config as config_mod
from ..statemachine import Phase

# Phase → Rich style string for row coloring
_PHASE_STYLE: dict[str, str] = {
    Phase.IMPLEMENTING.value:   "green",
    Phase.RESEARCHING.value:    "magenta",
    Phase.AWAITING_CI.value:    "cyan",
    Phase.IN_REVIEW.value:      "cyan",
    Phase.AWAITING_HUMAN.value: "yellow bold",
    Phase.DEGRADED.value:       "red bold",
    Phase.DONE.value:           "dim",
    Phase.TERMINATING.value:    "dim",
}


def _styled_row(*cells: str) -> tuple:
    """Return cells styled uniformly by the phase (cells[1]).

    Uses from_markup (not a literal Text()) so embedded markup — e.g. the PR
    cell's `[link=...]` — still renders/clicks correctly once phase-styled.
    """
    style = _PHASE_STYLE.get(cells[1], "")
    if not style:
        return cells
    return tuple(Text.from_markup(str(c), style=style) for c in cells)


def _fmt_age(age_s: int | None) -> str:
    if age_s is None:
        return "never"
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    return f"{age_s // 3600}h ago"


def _fmt_epoch(ts: float | None) -> str:
    if ts is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


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
    rate = doctor.get("spawns_last_hour") or {}
    spawns_total = rate.get("total", 0)
    budget = doctor.get("spawn_budget_per_hour", 0)
    runaway = doctor.get("runaway", False)
    runaway_str = ("[red bold]RUNAWAY[/red bold]" if runaway
                   else "[green]ok[/green]")
    spawns_str = (f"[red bold]{spawns_total}[/red bold]" if runaway
                  else str(spawns_total))

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
        f"  Spawns/hr:       {spawns_str} / budget {budget}",
        f"  Runaway:         {runaway_str}",
    ]
    return "\n".join(lines)


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
