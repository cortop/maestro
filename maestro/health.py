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
import os
import subprocess
from pathlib import Path

from . import claims, credentials, dispatcher, fleet, skills_install, spend as spend_mod, store
from . import snapshot as snap_mod
from .config import Config
from .statemachine import Phase

WINDOW_SECONDS = 3600

# Fallback heartbeat-stale threshold when no plist interval can be read (no
# LaunchAgent installed, or a plist override that doesn't exist). Matches the
# old hardcoded value, which itself assumed the common 300s default interval
# times this same factor.
DEFAULT_STALE_THRESHOLD = 1800
STALE_INTERVAL_FACTOR = 6  # missing this many consecutive sweeps looks stale


def spawn_rate(home: Path, now: float, window: int = WINDOW_SECONDS) -> dict:
    """Agent-equivalents observed in the trailing *window* seconds, from the
    ledger (GA-14).

    The ledger is rewritten only when something spawns, so ``recent`` can be
    stale during a quiet stretch -- always filter by window here rather than
    trusting the file to be pre-trimmed. Each entry carries its own
    ``dispatcher.spawn_weight`` (a legacy bare-timestamp entry, pre-GA-14, reads
    as weight 1 -- see ``dispatcher._ledger_entry_weight``); the total is the
    SUM of those weights, not a count of spawns.
    """
    ledger = store.read_json(dispatcher._spawn_ledger_path(home), {}) or {}
    by_key: dict[str, int] = {}
    for key, entry in ledger.items():
        recent = entry.get("recent", []) if isinstance(entry, dict) else []
        total = 0
        for e in recent:
            ts = dispatcher._ledger_entry_ts(e)
            if ts is not None and now - ts <= window:
                total += dispatcher._ledger_entry_weight(e)
        if total:
            by_key[key] = total
    return {"total": sum(by_key.values()), "by_key": by_key}


# GA-14: the unit `spawn_rate`/`spawn_budget` are now denominated in, surfaced
# by `maestro doctor` (see report()) so a human reading a bare number knows
# it's no longer a session count.
SPAWN_RATE_UNIT = "agent-equivalents"


def _budget_weight(cfg: Config, phase: str) -> int:
    """The per-spawn weight `spawn_budget` assumes for a key currently in
    *phase*, deliberately SMALLER than `dispatcher.spawn_weight`'s own
    preventive (worst-case) estimate for the same phase.

    `spawn_budget` already assumes every key spawns at its floor-capped max
    rate for a full hour -- compounding that with the ledger's own worst-case
    per-spawn weight (every one of those spawns maxing out `max_impl_turns`)
    would budget for the exact runaway pattern this detector exists to catch
    AS the healthy baseline, making the weighted detector no more sensitive
    than the session-counting one it replaces (see GA-14's spec, "the central
    design trap" -- multiplying both sides of `rate > budget` by the same
    constant never changes which side crosses first). Budgeting for HALF of
    the worst case is still generous headroom for legitimate QA convergence,
    while a key that's actually maxing out every single spawn -- the abuse
    case -- outgrows it much sooner than a bare session count would.
    """
    if phase != Phase.IMPLEMENTING.value:
        return 1
    full = dispatcher.spawn_weight(cfg, phase)
    return 1 + math.ceil((full - 1) / 2)


def spawn_budget(cfg: Config) -> int:
    """Fleet-wide agent-equivalents/hour the rate guards themselves permit,
    unless overridden by the ``runaway_spawns_per_hour`` knob (0 disables the
    runaway check; the override is already in the same unit `spawn_rate`
    reports, so it passes through unscaled).

    Default: for each current key, the per-key allowance ``ceil(3600 /
    effective_floor)`` -- exactly what ``spawn_floor`` permits one key --
    times that key's own ``_budget_weight`` (1 for every phase except
    ``implementing``, GA-14). This scales with board size AND composition (a
    board with no ``implementing`` tickets keeps the old session-counting
    budget exactly) and, critically, is not silenced by the same
    ``min_spawn_interval = 0`` misconfiguration the detector exists to catch:
    ``spawn_floor`` legitimately returns 0, so fall back to a sane per-key floor
    instead of dividing by zero.
    """
    if cfg.runaway_spawns_per_hour is not None:
        return int(cfg.runaway_spawns_per_hour)
    effective_floor = dispatcher.spawn_floor(cfg) or max(cfg.reconcile_steady_interval, 60)
    per_key = math.ceil(3600 / effective_floor)
    home = cfg.home
    total = 0
    for key in dispatcher.list_keys(home):
        phase = snap_mod.load(home, key).phase
        total += per_key * _budget_weight(cfg, phase)
    return total


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


def check_launchctl(cfg: Config, now: float | None = None, *, run=None) -> dict:
    # ``now`` is unused -- accepted only so this check has the same
    # ``(cfg, now, **kw)`` shape as every other CHECKS entry, letting
    # run_checks call the registry uniformly instead of special-casing it.
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
    """WARN (never blocks a spawn) when a ticket's spec/TicketCreated, or a
    ``[[scheduled]]`` task's ``repo`` field, names a repo that isn't a configured
    ``[repos.<name>]`` table -- e.g. a human typo in the ``repo:`` frontmatter
    line or a scheduled task. ``repos.resolve()`` silently falls back to the
    implicit default so the dispatcher can never wedge on this; this check
    surfaces the typo instead of letting it hide forever."""
    from . import repos as repos_mod

    home = cfg.home
    unknown = []
    for key in dispatcher.list_keys(home):
        name = repos_mod.bound_repo_name(home, key)
        if name and name not in cfg.repos:
            unknown.append({"key": key, "repo": name})
    for task in cfg.scheduled:
        name = task.get("repo")
        if name and name not in cfg.repos:
            unknown.append({"key": f"scheduled:{task.get('name')}", "repo": name})
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
    command every turn.

    GA-15: a repo-local miss isn't necessarily a real gap -- ``maestro
    install-commands --user`` (the alternative to vendoring into a repo this
    board doesn't own) puts the files in the user commands directory instead,
    which resolves from any cwd. Check there before flagging, via the same
    injectable ``skills_install.user_commands_dir`` the doctor tests override.
    """
    home = cfg.home
    bindings = dispatcher.referenced_repo_bindings(cfg, home, dispatcher.list_keys(home))
    user_dir = skills_install.user_commands_dir(cfg)
    user_has_skill = any(user_dir.glob("maestro-reconcile-*.md"))
    missing = []
    for name, binding in bindings.items():
        if not binding.path or not Path(binding.path).exists():
            continue
        commands_dir = Path(binding.path) / ".claude" / "commands"
        if any(commands_dir.glob("maestro-reconcile-*.md")):
            continue
        if user_has_skill:
            continue
        missing.append(name)
    status = "warn" if missing else "ok"
    detail = f"missing maestro-reconcile-*.md in: {', '.join(missing)}" if missing else "none"
    return {"name": "missing_reconcile_skill", "status": status, "detail": detail,
            "missing": missing}


def user_settings_path(cfg: Config | None = None) -> Path:
    """Where ``check_reconciler_permissions``'s user-scope settings layer lives --
    Claude Code resolves Bash permissions across a repo's
    ``.claude/settings.local.json``, its ``.claude/settings.json``, AND this
    user-scope file, so a grant present in any one of the three counts.

    Override precedence: ``MAESTRO_USER_SETTINGS_PATH`` env var >
    ``cfg.user_settings_path`` > ``~/.claude/settings.json`` -- mirrors
    ``skills_install.user_commands_dir``'s precedence so no test ever has to
    read (or risk clobbering) a developer's real ``~/.claude/settings.json``.
    """
    env = os.environ.get("MAESTRO_USER_SETTINGS_PATH")
    if env:
        return Path(env).expanduser()
    if cfg is not None and cfg.user_settings_path:
        return Path(cfg.user_settings_path).expanduser()
    return Path.home() / ".claude" / "settings.json"


def _settings_allow_deny(path: Path) -> tuple[list[str], list[str]]:
    """``(allow, deny)`` rule lists from one Claude Code settings.json-shaped
    file. Missing/malformed reads as ``([], [])``, never raises -- this check
    must never block a spawn."""
    data = store.read_json(path, {}) or {}
    perms = data.get("permissions", {}) if isinstance(data, dict) else {}
    if not isinstance(perms, dict):
        return [], []
    allow = perms.get("allow", [])
    deny = perms.get("deny", [])
    return (allow if isinstance(allow, list) else [],
            deny if isinstance(deny, list) else [])


def _repo_permission_surface(repo_path: Path, cfg: Config) -> tuple[set, set]:
    """Union of allow/deny rules across every settings layer Claude Code
    resolves permissions from for a reconciler working in *repo_path*: its own
    ``settings.local.json`` and ``settings.json``, plus the (injectable)
    user-scope ``settings.json`` -- a grant present in ANY layer satisfies the
    requirement."""
    allow: set[str] = set()
    deny: set[str] = set()
    for path in (
        repo_path / ".claude" / "settings.local.json",
        repo_path / ".claude" / "settings.json",
        user_settings_path(cfg),
    ):
        a, d = _settings_allow_deny(path)
        allow.update(a)
        deny.update(d)
    return allow, deny


def check_reconciler_permissions(cfg: Config, now: float) -> dict:
    """WARN (never blocks a spawn) when a repo bound by a current ticket has no
    Claude Code permission grant for the reconciler's whole Bash surface
    (``dispatcher.RECONCILER_REQUIRED_TOOLS``) -- the "This command needs your
    approval to run." stall (GA-16's Intent) that the dispatcher never
    observes (it doesn't read a spawned session's exit status), so today only
    the no-progress watchdog eventually reacts, after ~20 full ``claude -p``
    sessions per ticket, and blames the reconciler rather than permissions.

    Moot (reports ``ok``) when the home's ``permission_mode`` is
    ``bypassPermissions`` -- Claude Code never consults a settings file in
    that mode, so there is nothing to check.

    Modeled byte-for-byte on ``check_missing_reconcile_skill`` above: iterate
    ``dispatcher.referenced_repo_bindings``, skip a binding whose path doesn't
    exist (fails open, same as that check and ``repo_preflight_all``).
    """
    if cfg.permission_mode == "bypassPermissions":
        return {"name": "reconciler_permissions", "status": "ok",
                "detail": "permissions bypassed for this home (permission_mode = bypassPermissions)",
                "missing_by_repo": {}}

    home = cfg.home
    bindings = dispatcher.referenced_repo_bindings(cfg, home, dispatcher.list_keys(home))
    required = dispatcher.RECONCILER_REQUIRED_TOOLS
    missing_by_repo: dict[str, list[str]] = {}
    denied_by_repo: dict[str, list[str]] = {}
    for name, binding in bindings.items():
        if not binding.path or not Path(binding.path).exists():
            continue
        allow, deny = _repo_permission_surface(Path(binding.path), cfg)
        missing = [tool for tool in required if tool not in allow or tool in deny]
        if missing:
            missing_by_repo[name] = missing
        denied = [tool for tool in missing if tool in deny]
        if denied:
            denied_by_repo[name] = denied

    status = "warn" if missing_by_repo else "ok"
    if missing_by_repo:
        parts = []
        for name, tools in missing_by_repo.items():
            note = f"{name}: missing {', '.join(tools)}"
            if name in denied_by_repo:
                note += f" (denied by settings: {', '.join(denied_by_repo[name])})"
            parts.append(note)
        detail = "; ".join(parts)
    else:
        detail = "all referenced repos grant the full reconciler surface"
    return {"name": "reconciler_permissions", "status": status, "detail": detail,
            "missing_by_repo": missing_by_repo}


def check_spawn_floor(cfg: Config, now: float) -> dict:
    """WARN when the effective per-key spawn floor (``dispatcher.spawn_floor``,
    i.e. ``min_spawn_interval``) is 0 -- the setting has no other surface (GA-8):
    a typo or a debugging override left in place is otherwise invisible outside
    ``config.toml``. With the floor off, only ``max_concurrency`` x sweep cadence
    still bounds the fleet -- ``health.spawn_budget``'s fallback (health.py:62)
    keeps the runaway detector honest regardless, so this check is advisory
    visibility for the setting, not a second brake on top of it."""
    floor = dispatcher.spawn_floor(cfg)
    disabled = floor == 0
    detail = (f"spawn floor is 0 (disabled) -- only max_concurrency x sweep cadence "
              f"bounds the fleet" if disabled else f"spawn floor is {floor}s")
    return {"name": "spawn_floor", "status": "warn" if disabled else "ok",
            "detail": detail, "floor_s": floor}


def check_daily_spend(cfg: Config, now: float) -> dict:
    """GA-11: surface the daily spend meter/ceiling as a check, beside the
    top-level ``spend_*`` payload fields. WARN when the meter can't attribute
    cost at all (``session_log_format`` isn't ``stream-json`` -- see
    ``maestro/spend.py``'s module docstring: this must read as unavailable,
    never a silent ``$0.00``). FAIL when today's folded spend has already
    reached ``daily_spend_ceiling_usd`` -- the same signal ``dispatch()``'s
    gate (``spend.over_ceiling``) acts on, surfaced here so a human sees it
    without waiting for a blocked sweep.

    RB-8: an unset ``daily_spend_ceiling_usd`` is ALSO a WARN, not ``ok`` --
    it means the fleet's one hard cost guard is armed in code and disarmed in
    practice (exactly the 2026-08-08 dogfood-board finding: unset for the
    whole period since GA-11 merged, silently reading as a passing check).
    ``over_ceiling`` itself keeps failing OPEN for a ``None`` ceiling
    (unchanged, dispatch() must still spawn) -- this only changes how loud the
    *visibility* signal is, per this ticket's Intent."""
    st = spend_mod.status(cfg, now)
    if st["unavailable"]:
        return {"name": "daily_spend", "status": "warn",
                "detail": "spend unavailable: session_log_format is not stream-json",
                "today_usd": None, "ceiling_usd": st["ceiling_usd"]}
    ceiling = st["ceiling_usd"]
    if ceiling is None:
        return {"name": "daily_spend", "status": "warn",
                "detail": (f"no daily_spend_ceiling_usd configured -- ${st['today_usd']:.2f} "
                           "spent today, uncapped"),
                "today_usd": st["today_usd"], "ceiling_usd": None}
    over = spend_mod.over_ceiling(cfg, now)
    detail = over or f"${st['today_usd']:.2f} of ${float(ceiling):.2f} ceiling"
    return {"name": "daily_spend", "status": "fail" if over else "ok", "detail": detail,
            "today_usd": st["today_usd"], "ceiling_usd": ceiling}


def check_gh_credential_reachability(cfg: Config, now: float, *, run=None) -> dict:
    """WARN (never blocks a spawn) per ``[repos.<name>]`` table that names a
    ``slug``: whether the resolved gh credential (this repo's own
    ``gh_account``/``token_env``, or the ambient account when neither is set --
    see ``maestro.credentials``) can actually see that repo, via ``gh repo
    view <slug>`` under the SAME env overlay a spawn/``sync_vcs`` poll for
    that repo would use.

    Injectable subprocess boundary (``run``, defaulting to ``subprocess.run``)
    so the test suite never shells a real ``gh`` -- also threaded into
    ``credentials.resolve`` for the ``gh_account`` case, so ONE fake covers
    both the token lookup and the reachability probe. Skips entirely (``ok``,
    empty ``unreachable``) when no ``[repos.*]`` table names a slug -- never a
    network call on a home with nothing configured.
    """
    run = run or subprocess.run
    unreachable: dict[str, str] = {}
    checked = 0
    for name, table in cfg.repos.items():
        slug = table.get("slug")
        if not slug:
            continue
        checked += 1
        cred = credentials.resolve(table.get("gh_account"), table.get("token_env"), run=run)
        if not cred.ok:
            unreachable[name] = f"credential unresolvable: {cred.error}"
            continue
        env = dict(os.environ)
        if cred.env:
            env.update(cred.env)
        try:
            p = run(["gh", "repo", "view", slug, "--json", "name"],
                   capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            unreachable[name] = f"{type(e).__name__}: {e}"
            continue
        if p.returncode != 0:
            unreachable[name] = (p.stderr or "gh repo view failed").strip()
    status = "warn" if unreachable else "ok"
    if unreachable:
        detail = "; ".join(f"{name}: {reason}" for name, reason in unreachable.items())
    elif checked:
        detail = f"{checked} repo(s) reachable"
    else:
        detail = "no [repos.*] table names a slug"
    return {"name": "gh_credential_reachability", "status": status, "detail": detail,
            "unreachable": unreachable}


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
# Every entry is called uniformly as (cfg, now, **kw) -- run_checks below
# iterates this tuple directly rather than slicing it, so prepending,
# appending, or reordering an entry here is enough to include it; only
# check_heartbeat's plist override is special-cased, by identity, since it's
# the one check with a caller-supplied kwarg to thread through.
CHECKS = (check_heartbeat, check_backup_age, check_claim_age, check_dead_letters,
          check_depends_on, check_repo_preflight, check_unknown_repo_bindings,
          check_missing_reconcile_skill, check_reconciler_permissions, check_spawn_floor,
          check_daily_spend, check_gh_credential_reachability, check_launchctl)


def run_checks(cfg: Config, now: float, *, plist=None) -> list[dict]:
    return [check(cfg, now, plist=plist) if check is check_heartbeat else check(cfg, now)
            for check in CHECKS]


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
    spend_status = spend_mod.status(cfg, now)
    return {
        "heartbeat": hb,
        "heartbeat_age_s": age,
        "dead_letters": [p.stem for p in dead],
        "stale": age is not None and age > threshold,
        "spawns_last_hour": rate,
        "spawn_rate_unit": SPAWN_RATE_UNIT,
        "throttled_last_sweep": hb.get("throttled", 0),
        "spawn_budget_per_hour": budget,
        "spawn_floor_s": dispatcher.spawn_floor(cfg),
        "runaway": bool(budget) and rate["total"] > budget,
        # GA-11: added BESIDE the spawn-rate fields above, never folded into
        # them -- GA-14 re-denominates spawns_last_hour/spawn_budget_per_hour
        # next and needs those untouched.
        "spend_today_usd": spend_status["today_usd"],
        "spend_ceiling_usd": spend_status["ceiling_usd"],
        "spend_unavailable": spend_status["unavailable"],
        "spend_unattributed_sessions": spend_status["unattributed_sessions"],
        "paused": hb.get("paused", False),
        "checks": checks,
    }
