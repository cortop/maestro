"""Pure Rich-markup render/format helpers shared by the app and screens."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.text import Text

from .. import config as config_mod
from ..statemachine import Phase

# Phase → Rich style string for row coloring
_PHASE_STYLE: dict[str, str] = {
    Phase.IMPLEMENTING.value:   "green",
    Phase.VERIFYING.value:      "cyan",
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


def _render_badge(status: dict, provider: dict | None = None, nudge: bool = True) -> str:
    """Compact fleet state for the header: on/off, interval, heartbeat age.

    T-89 (AC1): *provider* -- ``health.check_provider_availability``'s own
    result -- rides along here so a provider/network outage is visible on the
    ALWAYS-visible main board, not only behind the `F` FleetScreen panel
    (``show=False``). ``None`` (no result yet, e.g. the very first render
    before the badge worker's first tick lands) renders exactly as before --
    no provider segment at all, never a false "ok".
    """
    dot = "[green]●[/green] up" if status.get("loaded") else "[red]○[/red] down"
    interval = status.get("interval")
    interval_str = f"{interval}s" if interval else "—"
    hb = _fmt_age(status.get("heartbeat_age_s"))
    badge = f"{dot}  [dim]int[/dim] {interval_str}  [dim]hb[/dim] {hb} "
    if status.get("paused"):
        badge = f"[yellow bold]⏸ PAUSED[/yellow bold]  {badge}"
    state = (provider or {}).get("state")
    if state == "no_network":
        badge = f"[red bold]NO NETWORK[/red bold]  {badge}"
    elif state == "erroring":
        badge = f"[yellow bold]ERRORING[/yellow bold]  {badge}"
    if not nudge:
        badge = f"[dim]nudge:off[/dim]  {badge}"
    return badge


def _nudge_toast(key: str | None, report) -> tuple[str, str]:
    """T-153: decode a nudge sweep's ``DispatchReport`` into one (message,
    severity) toast. *key* is ``None`` for the unfiltered create sweep, which
    reports what it minted instead."""
    spawned = key in report.spawned if key else bool(report.spawned)
    if key is None:
        head = (f"minted {', '.join(report.minted)}" if report.minted
                else "nothing minted")
        if report.paused:
            return "fleet paused -- queued, will run on resume", "warning"
        return (head + (f"; spawned {', '.join(report.spawned)}" if report.spawned else ""),
                "information")
    if spawned:
        return f"{key}: spawned", "information"
    if report.paused:
        return f"{key}: fleet paused -- queued, will run on resume", "warning"
    if report.paused_until:
        until = datetime.fromtimestamp(report.paused_until).strftime("%H:%M")
        return f"{key}: rate-limited until {until}", "warning"
    if report.spend_ceiling_reason:
        return f"{key}: spend ceiling -- {report.spend_ceiling_reason}", "warning"
    if report.repo_blockers:
        return f"{key}: repo blocked ({'; '.join(report.repo_blockers)})", "warning"
    if report.runner_blockers:
        why = "; ".join(f"{r}: {m}" for r, m in report.runner_blockers.items())
        return f"{key}: runner blocked ({why})", "warning"
    if key in report.claimed:
        return f"{key}: already running", "information"
    if key in report.throttled:
        return f"{key}: throttled (spawn floor)", "warning"
    if key in report.capacity_skipped:
        return f"{key}: at capacity", "warning"
    return f"{key}: not due", "information"


def _find_check(doctor: dict, name: str) -> dict:
    """The one ``doctor["checks"]`` entry named *name*, or ``{}`` if the
    registry doesn't carry it (an older cached doctor payload in a test, say)
    -- callers read fields off the result with ``.get`` so a missing check
    renders as the same "nothing wrong" default an ``ok`` one would."""
    for check in doctor.get("checks", []):
        if check.get("name") == name:
            return check
    return {}


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

    # MTO-8: the provider-availability check's three states -- ok / erroring
    # (network up, provider degraded) / no_network (this box is offline) --
    # rendered distinguishably so the operator's next action (nothing / wait
    # on the provider / check this machine's connection) is unambiguous.
    provider = _find_check(doctor, "provider_availability")
    provider_state = provider.get("state", "ok")
    if provider_state == "no_network":
        provider_str = f"[red bold]NO NETWORK[/red bold] -- {provider.get('detail', '')}"
    elif provider_state == "erroring":
        provider_str = f"[yellow bold]ERRORING[/yellow bold] -- {provider.get('detail', '')}"
    else:
        provider_str = "[green]ok[/green]"

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
        f"  Provider:        {provider_str}",
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
        f"  runner:             {cfg.runner}" + (f" (model: {cfg.runner_model})" if cfg.runner_model else ""),
        f"  runner_enabled:     {', '.join(cfg.runner_enabled) if cfg.runner_enabled else '—'}",
        "",
        "[bold]Providers[/bold]",
        "",
    ]
    for k, v in providers.items():
        lines.append(f"  {k + ':':20} {v}")
    return "\n".join(lines)


_DEP_COLORS = ("success", "warning", "error")  # blocking depth 0 / 1 / >=2


def _dep_color(depth: int, palette: dict) -> str:
    return palette.get(_DEP_COLORS[min(depth, 2)], ("green", "orange1", "red")[min(depth, 2)])


def _render_dep_header(graph, palette: dict) -> Text:
    """Per-color ticket counts (+ a red banner for cycles) for the deps screen."""
    counts = [0, 0, 0]
    for d in graph.depth.values():
        counts[min(d, 2)] += 1
    out = Text()
    for i, label in enumerate(("can start", "one step away", "stuck behind a chain")):
        out.append("● ", style=_dep_color(i, palette))
        out.append(f"{counts[i]} {label}   ")
    if graph.cycles:
        out.append("\n")
        out.append("cycles: " + "; ".join(" → ".join(c) for c in graph.cycles), style=_dep_color(2, palette))
    return out


def _dep_label(graph, key: str, palette: dict, in_cycle: bool = False) -> Text:
    """One tree row: colored key, title, phase, ⧗ when blocked, done-dep count, missing deps."""
    node = graph.nodes[key]
    color = _dep_color(graph.depth[key], palette)
    out = Text()
    out.append("⟳ " if in_cycle else "● ", style=color)
    out.append(key, style=f"bold {color}")
    out.append(f"  {node.title}  ")
    out.append(f"[{node.phase}]", style=_PHASE_STYLE.get(node.phase, ""))
    if node.blocked:
        out.append(" ⧗")
    if node.done_deps:
        out.append(f" ({node.done_deps} deps done)", style="dim")
    if node.missing:
        out.append(f" missing: {', '.join(node.missing)}", style="red")
    return out


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[int]) -> str:
    top = max(values, default=0)
    if top <= 0:
        return _SPARK[0] * len(values)
    return "".join(_SPARK[min(len(_SPARK) - 1, round(v / top * (len(_SPARK) - 1)))]
                   for v in values)


def _render_pulse(p: dict) -> str:
    """T-174: the one-line `#pulse` strip from the worker's read-only `p` dict."""
    if p.get("error"):
        return "[red]pulse: config error[/red]"
    total, budget = p["spawns"], p["budget"]
    spawns = f"spawns {total}/{budget} /hr"
    if p["runaway"]:
        spawns = f"[red bold]{spawns}[/red bold]"
    spend = p["spend"]
    if spend["unavailable"]:
        spend_str = "[yellow]unavailable[/yellow]"
    else:
        today, ceiling = spend["today_usd"], spend["ceiling_usd"]
        today_str = f"${today:.2f}" if today is not None else "—"
        if ceiling is None:
            spend_str = f"{today_str} / [yellow]no cap[/yellow]"
        else:
            ceiling = float(ceiling)
            fracs = [float(f) for f in p["warn_fractions"]]
            if today is not None and today >= ceiling:
                today_str = f"[red bold]{today_str}[/red bold]"
            elif today is not None and fracs and today >= min(fracs) * ceiling:
                today_str = f"[yellow]{today_str}[/yellow]"
            spend_str = f"{today_str} / ${ceiling:.2f}"
    hb = p["heartbeat"]
    counts = "·".join(str(hb.get(k, "—")) for k in ("active", "due", "throttled"))
    parts = [_sparkline(p["buckets"]), spawns, spend_str, f"{counts} (active·due·throttled)"]
    if p.get("paused_until") is not None:
        until = datetime.fromtimestamp(p["paused_until"], tz=timezone.utc).strftime("%H:%M")
        parts.append(f"[yellow bold]rate-limited → {until}[/yellow bold]")
    return "  ".join(parts)
