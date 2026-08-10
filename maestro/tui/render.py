"""Pure Rich-markup render/format helpers shared by the app and screens."""
from __future__ import annotations

import time

from rich.text import Text

from .. import config as config_mod
from ..statemachine import Phase

# Phase → Rich style string for row coloring
_PHASE_STYLE: dict[str, str] = {
    Phase.IMPLEMENTING.value:   "green",
    Phase.QA.value:             "blue",
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
    badge = f"{dot}  [dim]int[/dim] {interval_str}  [dim]hb[/dim] {hb} "
    if status.get("paused"):
        badge = f"[yellow bold]⏸ PAUSED[/yellow bold]  {badge}"
    return badge


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
    # GA-14: spawns_last_hour/spawn_budget_per_hour are agent-equivalents now,
    # not a bare session count -- label the line with the unit doctor reports
    # so "Spawns/hr" is never silently redefined out from under a reader.
    spawn_unit = doctor.get("spawn_rate_unit") or "sessions"
    spawn_unit_short = "agent-equiv" if spawn_unit == "agent-equivalents" else spawn_unit
    floor = doctor.get("spawn_floor_s")
    floor_str = ("[yellow]0 (disabled)[/yellow]" if floor == 0
                 else "—" if floor is None else f"{floor}s")
    runaway = doctor.get("runaway", False)
    runaway_str = ("[red bold]RUNAWAY[/red bold]" if runaway
                   else "[green]ok[/green]")
    spawns_str = (f"[red bold]{spawns_total}[/red bold]" if runaway
                  else str(spawns_total))
    paused = status.get("paused", False)
    paused_str = "[yellow bold]yes[/yellow bold]" if paused else "no"
    spend_unavailable = doctor.get("spend_unavailable", False)
    spend_today = doctor.get("spend_today_usd")
    spend_ceiling = doctor.get("spend_ceiling_usd")
    if spend_unavailable:
        spend_str = "[yellow]unavailable (session_log_format != stream-json)[/yellow]"
    else:
        today_str = f"${spend_today:.2f}" if spend_today is not None else "—"
        # RB-8: an unset ceiling reads as an explicit "no cap" warning, matching
        # the wording style of the `unavailable` branch above -- never a blank
        # or an omitted value, which is how the ceiling stayed unset unnoticed
        # on this very board (see health.check_daily_spend).
        ceiling_str = ("[yellow]no cap[/yellow]" if spend_ceiling is None
                       else f"${float(spend_ceiling):.2f}")
        over = (spend_ceiling is not None and spend_today is not None
                and spend_today >= float(spend_ceiling))
        spend_str = (f"[red bold]{today_str}[/red bold] / {ceiling_str}" if over
                     else f"{today_str} / {ceiling_str}")

    lines = [
        "[bold]Fleet & Health[/bold]",
        "",
        f"  Loaded:          {loaded}",
        f"  Heartbeat:       {age}",
        f"  Interval:        {interval_str}",
        f"  Label:           {label}",
        f"  Paused:          {paused_str}",
    ]
    if paused:
        until = _fmt_epoch(status.get("pause_until"))
        reason = status.get("pause_reason") or "—"
        lines.append(f"    until:         {until}")
        lines.append(f"    reason:        {reason}")
    lines += [
        "",
        "[bold]Doctor[/bold]",
        "",
        f"  Stale:           {stale_str}",
        f"  Dead letters:    {dead_str}",
        f"  Spawns/hr ({spawn_unit_short}): {spawns_str} / budget {budget}",
        f"  Spawn floor:     {floor_str}",
        f"  Runaway:         {runaway_str}",
        f"  Spend today:     {spend_str}",
    ]
    rl = doctor.get("rate_limit") or {}
    if rl.get("paused"):
        until_ts = rl.get("paused_until")
        until_str = time.strftime("%H:%M", time.localtime(until_ts)) if until_ts else "?"
        lines.append(f"  Rate limit:      [red bold]paused until {until_str}[/red bold]")
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
