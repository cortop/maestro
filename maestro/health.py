"""Fleet-wide health: liveness (heartbeat, dead-letters) plus a spawn-rate budget
that trips ``runaway`` when observed spawns/hour exceed what the rate guards
(``dispatcher.spawn_floor``) themselves permit. The existing liveness signals only
answer "is the dispatcher alive"; this answers "is it doing too much" (the
2026-07-19 incident: fresh heartbeat, zero dead letters, 21,731 no-op spawns).

``report()`` also runs a small check registry (L-12) surfacing things a stale
heartbeat alone won't: backup age, oldest live claim, launchd's last exit code,
dead-letter ages, and ``dependsOn`` graph validation (missing keys / cycles --
previously a typo'd or cyclic dependency stranded a ticket in ``blocked-dep``
forever with zero signal).
"""
from __future__ import annotations

import math
from pathlib import Path

from . import claims, dispatcher, fleet, store
from .config import Config

WINDOW_SECONDS = 3600

# Fallback heartbeat-stale threshold when no plist interval can be read (no
# LaunchAgent installed, or a plist override that doesn't exist). Matches the
# old hardcoded value, which itself assumed the common 300s default interval
# times this same factor.
DEFAULT_STALE_THRESHOLD = 1800
STALE_INTERVAL_FACTOR = 6  # missing this many consecutive sweeps looks stale


def spawn_rate(home: Path, now: float, window: int = WINDOW_SECONDS) -> dict:
    """Spawns actually observed in the trailing *window* seconds, from the ledger.

    The ledger is rewritten only when something spawns, so ``recent`` can be
    stale during a quiet stretch -- always filter by window here rather than
    trusting the file to be pre-trimmed. Legacy bare-float entries (pre-history
    ledgers) carry no history and contribute zero to the rate.
    """
    ledger = store.read_json(dispatcher._spawn_ledger_path(home), {}) or {}
    by_key: dict[str, int] = {}
    for key, entry in ledger.items():
        recent = entry.get("recent", []) if isinstance(entry, dict) else []
        count = sum(1 for t in recent if isinstance(t, (int, float)) and now - t <= window)
        if count:
            by_key[key] = count
    return {"total": sum(by_key.values()), "by_key": by_key}


def spawn_budget(cfg: Config) -> int:
    """Fleet-wide spawns/hour the rate guards themselves permit, unless overridden
    by the ``runaway_spawns_per_hour`` knob (0 disables the runaway check).

    Default: the per-key allowance ``ceil(3600 / effective_floor)`` -- exactly
    what ``spawn_floor`` permits one key -- times the number of tickets. This
    scales with board size and, critically, is not silenced by the same
    ``min_spawn_interval = 0`` misconfiguration the detector exists to catch:
    ``spawn_floor`` legitimately returns 0, so fall back to a sane per-key floor
    instead of dividing by zero.
    """
    if cfg.runaway_spawns_per_hour is not None:
        return int(cfg.runaway_spawns_per_hour)
    effective_floor = dispatcher.spawn_floor(cfg) or max(cfg.reconcile_steady_interval, 60)
    n_keys = len(dispatcher.list_keys(cfg.home))
    return n_keys * math.ceil(3600 / effective_floor)


def stale_threshold(home: Path | None = None, *, plist=None) -> int:
    """Heartbeat-stale threshold, derived from the actual installed plist's
    ``StartInterval`` rather than a hardcoded guess -- a fleet run at a
    non-default cadence (``maestro fleet up --interval N``) used to be flagged
    stale (or not) against a threshold that had nothing to do with its real
    sweep rate. *home* resolves which home's (possibly slugged) plist to read
    when ``plist`` isn't given an explicit override."""
    interval = fleet._interval_from_plist(plist, home=home)
    if not interval:
        return DEFAULT_STALE_THRESHOLD
    return interval * STALE_INTERVAL_FACTOR


def check_heartbeat(cfg: Config, now: float, *, plist=None) -> dict:
    home = cfg.home
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(now - hb["epoch"]) if hb.get("epoch") else None
    threshold = stale_threshold(home, plist=plist)
    stale = age is not None and age > threshold
    return {
        "name": "heartbeat", "status": "fail" if stale else "ok",
        "detail": f"heartbeat age {age}s (threshold {threshold}s)" if age is not None
                  else "no heartbeat yet",
        "heartbeat": hb, "age_s": age, "threshold_s": threshold, "stale": stale,
    }


def check_backup_age(cfg: Config, now: float) -> dict:
    if not cfg.backup_interval or cfg.backup_interval <= 0:
        return {"name": "backup_age", "status": "ok", "detail": "backups disabled", "age_s": None}
    cursor = store.read_json(cfg.home / "derived" / ".backup_cursor.json", {}) or {}
    epoch = cursor.get("epoch")
    age = round(now - epoch) if epoch else None
    stale = age is None or age > cfg.backup_interval * 2
    return {
        "name": "backup_age", "status": "warn" if stale else "ok",
        "detail": f"last backup {age}s ago" if age is not None else "no backup yet",
        "age_s": age,
    }


def check_claim_age(cfg: Config, now: float) -> dict:
    ages = {k: now - c.get("epoch", now) for k, c in claims.all_claims(cfg.home).items()}
    if not ages:
        return {"name": "claim_age", "status": "ok", "detail": "no live claims",
                "oldest_key": None, "oldest_age_s": None}
    oldest_key, oldest_age = max(ages.items(), key=lambda kv: kv[1])
    threshold = cfg.max_session_seconds or None
    warn = bool(threshold) and oldest_age > threshold
    return {
        "name": "claim_age", "status": "warn" if warn else "ok",
        "detail": f"oldest claim {oldest_key} is {round(oldest_age)}s old",
        "oldest_key": oldest_key, "oldest_age_s": round(oldest_age),
    }


def check_launchctl(cfg: Config, *, run=None) -> dict:
    kwargs = {"run": run} if run is not None else {}
    code = fleet.last_exit_code(cfg.home, **kwargs)
    fail = code is not None and code != 0
    return {
        "name": "launchctl", "status": "fail" if fail else "ok",
        "detail": f"last exit code {code}" if code is not None else "not loaded",
        "last_exit_code": code,
    }


def check_dead_letters(cfg: Config, now: float) -> dict:
    dl_dir = cfg.home / "tickets" / "_deadletter"
    ages = {}
    if dl_dir.exists():
        for p in dl_dir.glob("*.md"):
            try:
                ages[p.stem] = round(now - p.stat().st_mtime)
            except OSError:
                continue
    return {
        "name": "dead_letters", "status": "warn" if ages else "ok",
        "detail": f"{len(ages)} dead-lettered ticket(s)" if ages else "none",
        "ages_s": ages,
    }


def _key_exists_anywhere(home: Path, key: str) -> bool:
    """A dependency is only genuinely *missing* if it never existed -- a
    finished, archived ticket (``ops.archive_done``) is a legitimate satisfied
    dependency, not a typo."""
    if store.ticket_dir(home, key).exists():
        return True
    return (home / "tickets" / "_archive" / key).exists()


def _depends_on_graph(home: Path) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for key in dispatcher.list_keys(home):
        spec_file = store.spec_path(home, key)
        graph[key] = (dispatcher.parse_depends_on(spec_file.read_text(encoding="utf-8"))
                      if spec_file.exists() else [])
    return graph


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """DFS cycle detection over the dependsOn graph; each cycle is reported as
    the key path from its first repeated node back to itself."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}
    path: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color.get(dep) == GRAY:
                i = path.index(dep)
                cycles.append(path[i:] + [dep])
            elif color.get(dep) == WHITE:
                visit(dep)
        path.pop()
        color[node] = BLACK

    for node in list(graph):
        if color[node] == WHITE:
            visit(node)
    return cycles


def check_repo_preflight(cfg: Config, now: float) -> dict:
    """Live per-repo preflight (MR-5) across every repo referenced by any
    current ticket, plus the implicit default -- what a human sees in
    ``maestro doctor`` right now, independent of the last sweep's (possibly
    stale) heartbeat verdict. A configured-but-unreferenced ``[repos.*]``
    table never appears here (referenced-only, same as the dispatcher's own
    per-sweep probe)."""
    keys = dispatcher.list_keys(cfg.home)
    verdict = dispatcher.repo_preflight_all(cfg, cfg.home, keys)
    status = "fail" if not verdict["ok"] else "ok"
    if verdict["blockers_by_repo"]:
        detail = "; ".join(f"{name}: {', '.join(bl)}"
                           for name, bl in verdict["blockers_by_repo"].items())
    else:
        detail = "all referenced repos clean"
    return {"name": "repo_preflight", "status": status, "detail": detail,
            "blockers_by_repo": verdict["blockers_by_repo"]}


def check_unknown_repo_bindings(cfg: Config, now: float) -> dict:
    """WARN (never blocks a spawn) when a ticket's spec/TicketCreated names a
    repo that isn't a configured ``[repos.<name>]`` table -- e.g. a human typo
    in the ``repo:`` frontmatter line. ``repos.resolve()`` silently falls back
    to the implicit default so the dispatcher can never wedge on this; this
    check surfaces the typo instead of letting it hide forever."""
    from . import repos as repos_mod

    home = cfg.home
    unknown = []
    for key in dispatcher.list_keys(home):
        name = repos_mod.bound_repo_name(home, key)
        if name and name not in cfg.repos:
            unknown.append({"key": key, "repo": name})
    status = "warn" if unknown else "ok"
    detail = (f"{len(unknown)} ticket(s) bound to an unconfigured repo name"
              if unknown else "none")
    return {"name": "unknown_repo_bindings", "status": status, "detail": detail,
            "unknown": unknown}


def check_missing_reconcile_skill(cfg: Config, now: float) -> dict:
    """WARN (never blocks a spawn) when a repo bound by a current ticket is
    missing the per-phase reconcile commands under ``.claude/commands/`` (T-22
    split the single ``maestro-reconcile.md`` into
    ``maestro-reconcile-<phase>.md`` files for progressive disclosure) -- a
    reconciler spawned into that repo's worktree would hit an undefined slash
    command every turn."""
    home = cfg.home
    bindings = dispatcher.referenced_repo_bindings(cfg, home, dispatcher.list_keys(home))
    missing = []
    for name, binding in bindings.items():
        if not binding.path or not Path(binding.path).exists():
            continue
        commands_dir = Path(binding.path) / ".claude" / "commands"
        if not any(commands_dir.glob("maestro-reconcile-*.md")):
            missing.append(name)
    status = "warn" if missing else "ok"
    detail = f"missing maestro-reconcile-*.md in: {', '.join(missing)}" if missing else "none"
    return {"name": "missing_reconcile_skill", "status": status, "detail": detail,
            "missing": missing}


def check_depends_on(cfg: Config, now: float) -> dict:
    home = cfg.home
    graph = _depends_on_graph(home)
    missing = [{"key": key, "dep": dep}
               for key, deps in graph.items() for dep in deps
               if dep and dep not in graph and not _key_exists_anywhere(home, dep)]
    cycles = _find_cycles(graph)
    status = "fail" if cycles else ("warn" if missing else "ok")
    return {
        "name": "depends_on", "status": status,
        "detail": f"{len(missing)} missing dep(s), {len(cycles)} cycle(s)",
        "missing": missing, "cycles": cycles,
    }


# The check registry: cmd_doctor/report() run every entry and surface the
# results under "checks", in addition to the existing top-level fields kept
# for backward compatibility with the TUI fleet view and prior doctor output.
CHECKS = (check_heartbeat, check_backup_age, check_claim_age, check_dead_letters,
          check_depends_on, check_repo_preflight, check_unknown_repo_bindings,
          check_missing_reconcile_skill)


def run_checks(cfg: Config, now: float, *, plist=None) -> list[dict]:
    results = [check_heartbeat(cfg, now, plist=plist)]
    results += [check(cfg, now) for check in CHECKS[1:]]
    results.append(check_launchctl(cfg))
    return results


def report(cfg: Config, now: float, *, plist=None) -> dict:
    """The full doctor payload. ``cmd_doctor`` and the TUI fleet view both render
    this exact dict, so there is only one implementation of "is this too much".
    ``plist`` overrides the LaunchAgent plist path read for the heartbeat-stale
    threshold -- production always resolves the real one; tests inject a fake."""
    home = cfg.home
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(now - hb["epoch"]) if hb.get("epoch") else None
    dl_dir = home / "tickets" / "_deadletter"
    dead = list(dl_dir.glob("*.md")) if dl_dir.exists() else []
    rate = spawn_rate(home, now)
    budget = spawn_budget(cfg)
    checks = run_checks(cfg, now, plist=plist)
    threshold = stale_threshold(home, plist=plist)
    return {
        "heartbeat": hb,
        "heartbeat_age_s": age,
        "dead_letters": [p.stem for p in dead],
        "stale": age is not None and age > threshold,
        "spawns_last_hour": rate,
        "throttled_last_sweep": hb.get("throttled", 0),
        "spawn_budget_per_hour": budget,
        "runaway": bool(budget) and rate["total"] > budget,
        "paused": hb.get("paused", False),
        "checks": checks,
    }
