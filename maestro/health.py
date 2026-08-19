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
import re
import shutil
import subprocess
from pathlib import Path

from . import backup as backup_mod
from . import claims, credentials, dispatcher, event_log, fleet, pi_guard, runner_permissions, skills_install, spend as spend_mod, store
from . import sessions as sessions_mod, steplog
from . import snapshot as snap_mod
from .config import Config
from .gates import spec_runner
from .providers import ollama as ollama_mod
from .providers import pi as pi_mod
from .statemachine import Phase, TERMINAL_PHASES

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
    per-spawn weight would budget for the exact runaway pattern this detector
    exists to catch AS the healthy baseline, making the weighted detector no
    more sensitive than the session-counting one it replaces (see GA-14's
    spec, "the central design trap" -- multiplying both sides of `rate >
    budget` by the same constant never changes which side crosses first).
    Budgeting for HALF of the worst case is still generous headroom for
    legitimate QA convergence, while a key that's actually maxing out every
    single spawn -- the abuse case -- outgrows it much sooner than a bare
    session count would.

    RF-7 moved the variable-weight phase from `implementing` (which fanned out
    an unbounded `Agent`-tool QA loop in-session) to `qa` (which fans out at
    most ONE Standards-axis sub-agent, config-gated by `qa_standards_axis`) --
    every other phase, `implementing` included, now spawns no sub-agents and
    weighs 1 exactly, so it takes the same fast path as `qa` does when the
    Standards axis is off.
    """
    if phase != Phase.QA.value:
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
    ``qa``, GA-14/RF-7). This scales with board size AND composition (a
    board with no ``qa`` tickets keeps the old session-counting budget
    exactly) and, critically, is not silenced by the same
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
    """Grounded in the actual tarballs `backup.list_backups` finds -- never in
    `derived/.backup_cursor.json`, which only the dispatcher's periodic
    `backup.maybe_backup` writes and which a manual `maestro backup` or a wiped
    `backup_dir` leave dangerously wrong in either direction (T-88). A board
    that has never swept (no `derived/.heartbeat.json`) and has no tarballs
    reports its own non-warning state, mirroring `check_heartbeat`'s "no
    heartbeat yet" -> status ok contract above; a board that HAS swept and
    still has no tarballs is a real fault.
    """
    if not cfg.backup_interval or cfg.backup_interval <= 0:
        return {"name": "backup_age", "status": "ok", "detail": "backups disabled", "age_s": None}
    epoch = backup_mod.newest_backup_epoch(cfg)
    if epoch is None:
        swept = (cfg.home / "derived" / ".heartbeat.json").exists()
        return {
            "name": "backup_age", "status": "warn" if swept else "ok",
            "detail": "no backups found in backup_dir" if swept else "no backup yet -- board never swept",
            "age_s": None,
        }
    age = round(now - epoch)
    stale = age > cfg.backup_interval * 2
    return {
        "name": "backup_age", "status": "warn" if stale else "ok",
        "detail": f"last backup {age}s ago", "age_s": age,
    }


# QW-4: warn at this fraction of max_session_seconds, well before the watchdog's own
# reap threshold -- warning AT the reap threshold (the old behavior) means the watchdog
# already killed the claim by the time doctor could ever observe it as "warn", making
# the one signal that should surface a wedge unreachable in practice.
CLAIM_AGE_WARN_FRACTION = 0.5


def check_claim_age(cfg: Config, now: float) -> dict:
    claim_data = claims.all_claims(cfg.home)
    ages = {k: now - c.get("epoch", now) for k, c in claim_data.items()}
    if not ages:
        return {"name": "claim_age", "status": "ok", "detail": "no live claims",
                "oldest_key": None, "oldest_age_s": None, "stale_output_s": None}
    oldest_key, oldest_age = max(ages.items(), key=lambda kv: kv[1])
    threshold = cfg.max_session_seconds or None
    warn_threshold = threshold * CLAIM_AGE_WARN_FRACTION if threshold else None
    warn = bool(warn_threshold) and oldest_age > warn_threshold
    stale_output_s = None
    log_path = claim_data[oldest_key].get("log_path")
    if log_path:
        try:
            stale_output_s = round(now - Path(log_path).stat().st_mtime)
        except OSError:
            stale_output_s = None
    return {
        "name": "claim_age", "status": "warn" if warn else "ok",
        "detail": f"oldest claim {oldest_key} is {round(oldest_age)}s old",
        "oldest_key": oldest_key, "oldest_age_s": round(oldest_age),
        "stale_output_s": stale_output_s,
    }


def check_claim_no_output(cfg: Config, now: float) -> dict:
    """Distinct from ``check_claim_age`` above -- names the OTHER watchdog clock
    (``dispatcher.run_watchdog``'s no-output rule) so a wedged-but-young claim
    (fresh epoch, silent log) shows up under its own name instead of being
    invisible to `claim_age`, which only tracks wall-clock age. Exempt for a
    claim with no `log_path` (no output signal, same exemption the watchdog
    itself applies)."""
    timeout = cfg.no_output_timeout
    if not timeout:
        return {"name": "claim_no_output", "status": "ok", "detail": "no-output watchdog disabled",
                "stale_key": None, "stale_age_s": None}
    stale_key = None
    stale_age = None
    for key, claim in claims.all_claims(cfg.home).items():
        log_path = claim.get("log_path")
        if not log_path:
            continue
        try:
            age = now - os.stat(log_path).st_mtime
        except OSError:
            continue
        if age > timeout and (stale_age is None or age > stale_age):
            stale_key, stale_age = key, age
    warn = stale_key is not None
    return {
        "name": "claim_no_output", "status": "warn" if warn else "ok",
        "detail": (f"{stale_key} has produced no session output for {round(stale_age)}s"
                   if warn else "no stale-output claims"),
        "stale_key": stale_key, "stale_age_s": round(stale_age) if warn else None,
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


def check_phantom_keys(cfg: Config, now: float) -> dict:
    """WARN when a key `list_keys` knows about (an event log and/or a derived
    snapshot on disk) has no `spec.md` -- RB-17's shape: a reconciler was
    spawned in error for a key never minted via `maestro create`, and
    recorded real event-log history against it anyway (typically before
    RB-17's spawn/append guards existed -- `dispatcher._never_minted` and
    `ops._refuse_unminted` now stop a NEW one from ever getting this far).

    Flagged for manual triage only, never auto-cleaned here: the event log is
    the sole source of truth (see CLAUDE.md), so deleting one is a decision
    for a human, made via `maestro cmd <KEY> discard` -- which stays fully
    functional against a flagged key (RB-17 AC4; `_never_minted` is False for
    any key with real event-log history, so the dispatcher keeps sweeping it
    normally)."""
    home = cfg.home
    phantoms = sorted(key for key in dispatcher.list_keys(home)
                       if not store.spec_path(home, key).exists())
    status = "warn" if phantoms else "ok"
    detail = (f"{len(phantoms)} key(s) have event/snapshot data but no spec.md: "
              + ", ".join(phantoms)) if phantoms else "none"
    return {"name": "phantom_keys", "status": status, "detail": detail, "keys": phantoms}


def check_missing_acs(cfg: Config, now: float) -> dict:
    """T-80: WARN naming each non-terminal ticket whose spec.md parses to zero
    acceptance criteria (`snapshot.has_acs`) -- report-only, the safety net
    for a board that predates the dispatcher's own early due-gate
    (`dispatcher.dispatch`'s per-key park, checked before a session ever
    spawns), and for the rarer case of a ticket that reached a later phase
    before a human edit cleared its ACs back out from under it (the write-path
    half of that gate lives in `ops.set_phase`/`ops.qa_brief`). Skips a
    DONE/terminal ticket -- finished work is never flagged, matching the
    spec's "no spec rewrites, terminal tickets are never flagged" backward-
    compat note."""
    home = cfg.home
    flagged = []
    for key in dispatcher.list_keys(home):
        if Phase(snap_mod.load(home, key).phase) in TERMINAL_PHASES:
            continue
        spec_file = store.spec_path(home, key)
        if not spec_file.exists():
            continue
        if not snap_mod.has_acs(spec_file.read_text(encoding="utf-8")):
            flagged.append(key)
    status = "warn" if flagged else "ok"
    detail = (f"{len(flagged)} non-terminal ticket(s) have no acceptance criteria: "
              + ", ".join(flagged)) if flagged else "none"
    return {"name": "missing_acs", "status": status, "detail": detail, "keys": flagged}


# 2+ already means the exact same automated no-progress path tripped for the
# same key more than once -- the shape of the 2026-08-14 degraded-respawn-
# forever incident (OC-7, T-65), where it happened dozens of times per key.
WATCHDOG_LOOP_THRESHOLD = 2


def check_watchdog_loops(cfg: Config, now: float) -> dict:
    """WARN when a key has repeatedly tripped the no-progress watchdog
    (`dispatcher._allow_spawn`'s ``"watchdog: N spawns with no progress..."``
    ``Failed`` events) -- surfaced directly from the event log so the next
    instance of this shape (a phase wrongly classified as always-active, or
    any other spawn loop feeding the same watchdog) is visible in `maestro
    doctor` without a human reading raw session logs to notice the pattern.
    Counts every matching ``Failed`` a key has ever recorded, not just its
    most recent streak -- the incident this guards against reproduces the
    identical error text on every trip, so a lifetime count is exactly what
    makes the repetition visible."""
    home = cfg.home
    counts: dict[str, int] = {}
    for key in dispatcher.list_keys(home):
        n = sum(1 for e in event_log.read(home, key)
                if e.get("type") == "Failed"
                and str(e.get("payload", {}).get("error", "")).startswith("watchdog:"))
        if n >= WATCHDOG_LOOP_THRESHOLD:
            counts[key] = n
    status = "warn" if counts else "ok"
    detail = (f"{len(counts)} ticket(s) repeatedly tripping the no-progress watchdog"
              if counts else "none")
    return {"name": "watchdog_loops", "status": status, "detail": detail, "counts": counts}


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


def _runner_for_key(cfg: Config, key: str) -> str:
    """T-54: "the runner for key K" -- the decision this predicate encodes --
    for the two doctor checks below.

    Before T-54, a runner only ever meant something for the `implementing`
    phase (`dispatcher.resolve_runner` hard-forced `claude` everywhere else),
    so OC-1's original version of this function just resolved against
    ``Phase.IMPLEMENTING`` unconditionally. Phase-aware routing breaks that:
    a runner can now be admitted to other phases too
    (`dispatcher.runner_eligible_phases`), so no single hardcoded phase is
    still the right one to probe.

    What `check_missing_reconcile_skill`/`check_reconciler_permissions`
    actually need is simpler than "which phase": whether a spawn into this
    repo will EVER use a non-``claude`` runner, so its command-file location
    and permission surface for that runner are ready ahead of time -- not
    which specific phase triggers it. So "the runner for key K" here means:
    the runner *key*'s spec resolves to (`gates.spec_runner`, falling back to
    `cfg.runner`), UNLESS that runner's whole eligible-phase set
    (`dispatcher.runner_eligible_phases`) is empty -- in which case it can
    never actually be spawned for *key* regardless of what the spec says, so
    it's equivalent to `claude` for these checks' purposes."""
    runner, _ = spec_runner(cfg.home, key)
    runner = runner or cfg.runner
    if not dispatcher.runner_eligible_phases(cfg, runner):
        return "claude"
    return runner


def _runners_by_binding(cfg: Config, home: Path, keys) -> dict:
    """{repo binding name: runner} for the three runner-aware checks below --
    one runner per binding, resolved from whichever key(s) reference it (see
    `_runner_for_key`). A binding referenced by keys with different
    runners keeps the FIRST seen (mirrors `referenced_repo_bindings`'s own
    first-seen-wins rule) -- a real board binds one runner per repo in
    practice; this doesn't attempt to represent a mixed-runner repo. A binding
    with no referencing key (e.g. the implicit default on a board with no live
    tickets yet) is absent here -- callers fall back to `cfg.runner`.

    T-61: pi's own consumer (`_pi_permission_gap`) doesn't actually read
    *anything* keyed by binding name -- pi's guard extension is one
    board-wide install (`store.pi_agent_dir(cfg.home)`), not a per-repo file
    -- so a binding mixing a pi ticket with a claude/opencode one is not a
    silent gap for THAT check specifically: whichever runner wins first-seen,
    the pi-model/pi-binary checks beside it (`check_pi_models`,
    `check_runner_binary`) still see every ticket's OWN spec directly, never
    through this per-binding fold. The opencode-side checks keep the
    pre-existing limitation this docstring already documented."""
    from . import repos as repos_mod  # lazy: repos imports dispatcher at module load time

    out: dict = {}
    for key in keys:
        binding = repos_mod.resolve(cfg, home, key)
        out.setdefault(binding.name, _runner_for_key(cfg, key))
    return out


def check_missing_reconcile_skill(cfg: Config, now: float) -> dict:
    """WARN (never blocks a spawn) when a repo bound by a current ticket is
    missing one or more of the per-phase reconcile commands from ITS runner's
    own command location (T-22 split the single ``maestro-reconcile.md`` into
    ``maestro-reconcile-<phase>.md`` files for progressive disclosure) -- a
    reconciler spawned into that repo's worktree would hit an undefined slash
    command every turn.

    T-87: the predicate is a per-file set-diff against the full
    ``skills_install.PAYLOAD_NAMES`` roster, not ``any(...)`` over a glob --
    ``any()`` reads ``ok`` the moment ONE of the seven files exists, so a
    partially-installed command set (e.g. every phase but ``qa``) silently
    passed and a reconciler spawned into the ``qa`` phase hit an undefined
    slash command with no preflight to have caught it (the real-board install
    this ticket is named for). ``missing`` keeps its pre-existing shape --
    binding names, for `test_repo_preflight.py`/`test_install_commands.py` --
    and gains a sibling ``missing_files`` mapping each such binding to the
    actual absent filenames, which the WARN detail also names verbatim.

    GA-15: a repo-local miss isn't necessarily a real gap -- ``maestro
    install-commands --user`` (the alternative to vendoring into a repo this
    board doesn't own) puts the files in the user commands directory instead,
    which resolves from any cwd. The set-diff is against the UNION of the
    repo-local and user-scope directories' file sets, per file -- a file
    present in either location satisfies it for that file specifically, so a
    stray unrelated file in one location can no longer suppress a genuine gap
    in the other (unlike the old any()-per-directory check, which read the
    two locations as independently sufficient wholes).

    OC-1: runner-aware -- a binding whose referencing ticket(s) resolve to a
    non-``claude`` runner (`_runners_by_binding`) is checked against
    opencode's OWN command location (``.opencode/command/`` /
    ``skills_install.opencode_user_commands_dir``) instead of
    ``.claude/commands/`` -- checking the Claude-side location for an
    opencode-runner ticket would be a check of the wrong file, not a
    meaningful ok/warn.

    T-61: a pi binding is always satisfied, never checked against either
    location -- PI-8's ``PiCliSessions.spawn`` passes the reconcile prompt
    via ``--prompt-template`` at the package payload directly (never reads
    ``.claude/commands/`` or ``.opencode/command/``), so there is no
    per-repo command-file location for pi that could ever go missing.
    """
    home = cfg.home
    keys = dispatcher.list_keys(home)
    bindings = dispatcher.referenced_repo_bindings(cfg, home, keys)
    runners_by_binding = _runners_by_binding(cfg, home, keys)
    payload_names = set(skills_install.PAYLOAD_NAMES)
    user_dir = skills_install.user_commands_dir(cfg)
    user_files = {p.name for p in user_dir.glob("maestro-reconcile-*.md")}
    opencode_user_dir = skills_install.opencode_user_commands_dir(cfg)
    opencode_user_files = {p.name for p in opencode_user_dir.glob("maestro-reconcile-*.md")}
    missing = []
    missing_files: dict[str, list[str]] = {}
    for name, binding in bindings.items():
        if not binding.path or not Path(binding.path).exists():
            continue
        runner = runners_by_binding.get(name, cfg.runner)
        if runner == "pi":
            continue
        if runner == "claude":
            commands_dir = Path(binding.path) / ".claude" / "commands"
            user_files_here = user_files
        else:
            commands_dir = skills_install.opencode_repo_commands_dir(binding.path)
            user_files_here = opencode_user_files
        repo_files = {p.name for p in commands_dir.glob("maestro-reconcile-*.md")}
        gap = sorted(payload_names - (repo_files | user_files_here))
        if not gap:
            continue
        missing.append(name)
        missing_files[name] = gap
    status = "warn" if missing else "ok"
    if missing:
        detail = "missing maestro-reconcile-*.md in: " + "; ".join(
            f"{name} ({', '.join(missing_files[name])})" for name in missing)
    else:
        detail = "none"
    return {"name": "missing_reconcile_skill", "status": status, "detail": detail,
            "missing": missing, "missing_files": missing_files}


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


def _maestro_self_root() -> Path:
    """The repo this running maestro package's own source lives in -- the
    only binding ``dispatcher.MAESTRO_OWN_REPO_EXTRA_TOOLS`` (``.venv/bin/``)
    genuinely describes (MTO-5). A separate, monkeypatchable function rather
    than inlined into ``_is_maestro_own_repo`` so tests can simulate 'this
    binding IS maestro's own repo' without depending on where the test
    process itself happens to be installed from."""
    return Path(__file__).resolve().parents[1]


def _is_maestro_own_repo(path: Path) -> bool:
    """True when *path* resolves to the same repo ``_maestro_self_root``
    names -- i.e. this binding is maestro's own checkout, not a foreign
    yarn/Bazel-bound repo that has no ``.venv/bin/`` and never will."""
    try:
        return Path(path).resolve() == _maestro_self_root()
    except OSError:
        return False


def _missing_maestro_grant(allow: set, deny: set) -> list[str]:
    """MTO-5: dual acceptance for the 'maestro' requirement -- satisfied by
    EITHER the coarse ``dispatcher.MAESTRO_COARSE_GRANT`` wildcard (how a
    human grants it by hand in their own settings file; a different trust
    boundary than the spawner emitting it itself, AD-1's actual concern) OR
    the full narrowed ``dispatcher.maestro_verb_grant()`` set (the literal
    form GA-3 actually emits at spawn time). Returns ``[]`` when either is
    satisfied, else ``[MAESTRO_COARSE_GRANT]`` -- the coarse literal alone, so
    a genuinely-broken repo's report reads as one missing item, not ~24."""
    coarse = dispatcher.MAESTRO_COARSE_GRANT
    if coarse in allow and coarse not in deny:
        return []
    narrow = dispatcher.maestro_verb_grant()
    if all(tool in allow and tool not in deny for tool in narrow):
        return []
    return [coarse]


def _opencode_permission_gap(cfg: Config, repo_path: Path) -> str | None:
    """T-64: the opencode counterpart of `_missing_maestro_grant` above -- an
    opencode-runner binding's real permission surface is the generated
    ``opencode.jsonc`` (``skills_install._opencode_declarative_config``), not
    Claude Code's settings.json, so this reads THAT file instead: repo-scope
    first, falling back to user-scope, exactly like
    ``check_missing_reconcile_skill`` already does for the command-file check.
    Compared against what the installer would generate RIGHT NOW
    (``runner_permissions.opencode_permission_block``) byte-for-byte, so a
    missing file, a stale file (a ``config.toml`` edit since the last
    ``install-commands`` run), and a foreign hand-edited file are all reported
    the same way -- 'close but not exactly what would be generated now' must
    warn, not silently pass (QW-1's rule, same as the Claude-side check).
    Returns ``None`` when it matches, else a short human-readable reason.
    """
    for path in (skills_install.opencode_repo_config_path(repo_path),
                 skills_install.opencode_user_config_path(cfg)):
        parsed = skills_install.read_opencode_config(path)
        if parsed is None:
            continue
        if parsed.get("permission") == runner_permissions.opencode_permission_block(cfg.home):
            return None
        return f"opencode.jsonc permission block at {path} is missing or stale"
    return ("no opencode.jsonc found at "
            f"{skills_install.opencode_repo_config_path(repo_path)} or "
            f"{skills_install.opencode_user_config_path(cfg)} -- run `maestro install-commands`")


def _pi_permission_gap(cfg: Config) -> str | None:
    """T-61: pi's counterpart of `_opencode_permission_gap` above -- pi ships
    NO permission gate of its own (auto-approve by design, see
    `pi_guard`'s module docstring), so maestro's guard extension IS its
    entire permission surface, materialized once per board under
    `store.pi_agent_dir(cfg.home)` (never per-repo -- unlike Claude Code's
    settings.json or opencode's opencode.jsonc, a pi binding has no
    repo-local permission file of its own to read).

    RB-16 fix round: `PiCliSessions.spawn` now installs the guard's own
    extension tree into a PER-KEY subdirectory (`store.pi_agent_key_dir`),
    not the bare board-wide dir, so each real spawn's ``pi_guard_data.json``
    can carry its own phase-narrowed verb set without a race against a
    concurrently-spawned, differently-phased key (see that function's
    docstring). This check follows suit: ``ok`` once ANY key's extension has
    been installed under ``<pi_agent_dir>/by-key/*/`` -- the same "has pi ever
    actually been gated on this board" smoke signal as before, just resolved
    against the directory a real spawn now writes to.

    Read-only existence check -- never calls `pi_guard.install` itself (a
    doctor check must never write); `PiCliSessions.spawn` is what actually
    installs it, belt-and-suspenders re-verified there on every real spawn
    (see its own docstring). Returns ``None`` when at least one key's
    extension is already installed, else a short reason -- an ungated pi
    board must never report ``ok`` (the Note this check exists for: "a green
    doctor on an ungated pi board is the worst possible output")."""
    by_key = store.pi_agent_dir(cfg.home) / "by-key"
    if by_key.is_dir():
        for key_dir in by_key.iterdir():
            if pi_guard.extension_path(key_dir).exists():
                return None
    return f"pi guard extension not installed under {by_key} -- no pi session has spawned yet"


def check_reconciler_permissions(cfg: Config, now: float) -> dict:
    """WARN (never blocks a spawn) when a repo bound by a current ticket has no
    Claude Code permission grant for the reconciler's whole Bash surface
    (``dispatcher.RECONCILER_REQUIRED_TOOLS``, plus
    ``dispatcher.MAESTRO_OWN_REPO_EXTRA_TOOLS`` when the binding is maestro's
    own repo) -- the "This command needs your approval to run." stall (GA-16's
    Intent) that the dispatcher never observes (it doesn't read a spawned
    session's exit status), so today only the no-progress watchdog eventually
    reacts, after ~20 full ``claude -p`` sessions per ticket, and blames the
    reconciler rather than permissions.

    Moot (reports ``ok``) when the home's ``permission_mode`` is
    ``bypassPermissions`` -- Claude Code never consults a settings file in
    that mode, so there is nothing to check.

    The 'maestro' requirement is dual-accepted (``_missing_maestro_grant``);
    every other literal is matched exact-string -- an equivalent but
    differently-spelled grant (e.g. ``Bash(gh pr:*)`` + ``Bash(gh api:*)`` in
    place of ``Bash(gh:*)``) does NOT satisfy it, and the WARN detail says so.
    ``.venv/bin/`` is only required of the binding that resolves to maestro's
    own repo checkout (``_is_maestro_own_repo``) -- a foreign yarn/Bazel-bound
    repo is never measured against it (MTO-5).

    Modeled byte-for-byte on ``check_missing_reconcile_skill`` above: iterate
    ``dispatcher.referenced_repo_bindings``, skip a binding whose path doesn't
    exist (fails open, same as that check and ``repo_preflight_all``).

    OC-1/T-64: runner-aware. A binding whose referencing ticket(s) resolve to
    ``opencode`` (`_runners_by_binding`) is checked against ITS OWN runner-owned
    surface instead -- the generated ``opencode.jsonc`` (see
    ``skills_install._opencode_declarative_config`` /
    ``runner_permissions.opencode_permission_block``, the write path this
    check used to have nothing to read), via `_opencode_permission_gap` below.

    T-61: a binding whose runner is ``pi`` is checked against ITS OWN
    surface too -- the guard extension's on-disk presence (`_pi_permission_gap`)
    -- rather than folded into ``not_applicable_by_repo``. Before this, an
    ungated pi board (no session ever spawned, so `pi_guard.install` never
    ran) reported ``ok`` here, which is the worst possible doctor output for
    a runner that ships with NO permission gate of its own.

    A binding whose runner is neither ``claude``, ``opencode``, nor ``pi``
    (no such runner is registered today -- `dispatcher._REGISTERED_RUNNERS`
    -- but a future one could land before this check is updated for it) is
    reported ``not_applicable_by_repo`` with a named reason instead of being
    folded into ``missing_by_repo``/``ok`` -- reporting ``ok`` for a surface
    nothing here actually reads would claim a verification that never
    happened (the exact silent-``ok`` class QW-1 exists to prevent).
    """
    if cfg.permission_mode == "bypassPermissions":
        return {"name": "reconciler_permissions", "status": "ok",
                "detail": "permissions bypassed for this home (permission_mode = bypassPermissions)",
                "missing_by_repo": {}, "not_applicable_by_repo": {}}

    home = cfg.home
    keys = dispatcher.list_keys(home)
    bindings = dispatcher.referenced_repo_bindings(cfg, home, keys)
    runners_by_binding = _runners_by_binding(cfg, home, keys)
    literal_required = [tool for tool in dispatcher.RECONCILER_REQUIRED_TOOLS
                         if tool != dispatcher.MAESTRO_COARSE_GRANT]
    missing_by_repo: dict[str, list[str]] = {}
    denied_by_repo: dict[str, list[str]] = {}
    not_applicable_by_repo: dict[str, str] = {}
    for name, binding in bindings.items():
        if not binding.path or not Path(binding.path).exists():
            continue
        runner = runners_by_binding.get(name, cfg.runner)
        repo_path = Path(binding.path)
        if runner == "opencode":
            gap = _opencode_permission_gap(cfg, repo_path)
            if gap:
                missing_by_repo[name] = [gap]
            continue
        if runner == "pi":
            gap = _pi_permission_gap(cfg)
            if gap:
                missing_by_repo[name] = [gap]
            continue
        if runner != "claude":
            not_applicable_by_repo[name] = (
                f"runner {runner!r} enforces permissions through its own config, "
                "not Claude Code settings.json -- this check does not inspect it")
            continue
        allow, deny = _repo_permission_surface(repo_path, cfg)
        missing = _missing_maestro_grant(allow, deny)
        missing += [tool for tool in literal_required if tool not in allow or tool in deny]
        if _is_maestro_own_repo(repo_path):
            missing += [tool for tool in dispatcher.MAESTRO_OWN_REPO_EXTRA_TOOLS
                        if tool not in allow or tool in deny]
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
        detail = ("; ".join(parts) +
                   ". Matching is exact-string -- an equivalent but "
                   "differently-spelled grant (e.g. Bash(gh pr:*) in place of "
                   "Bash(gh:*)) is still reported missing.")
    else:
        detail = "all referenced repos grant the full reconciler surface"
    if not_applicable_by_repo:
        detail += ("; not inspected (non-claude runner): "
                   + ", ".join(f"{name} ({reason})" for name, reason in not_applicable_by_repo.items()))
    return {"name": "reconciler_permissions", "status": status, "detail": detail,
            "missing_by_repo": missing_by_repo, "not_applicable_by_repo": not_applicable_by_repo}


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


_RUNNER_FIELD_RE = re.compile(r"^([a-zA-Z_]\w*)\s*:\s*(.+)$")


def _spec_runner_fields(spec_text: str) -> tuple[str | None, str | None]:
    """Read ``runner:``/``runner_model:`` straight off a spec's loose frontmatter
    -- the same line-based scan ``gates.parse_spec_overrides`` uses (stop at the
    first ``##`` header), kept as its own small helper here rather than folded
    into that function: canonicalizing those two fields into
    ``parse_spec_overrides``'s five-field allowlist is RF-2's (T-31) job, with
    its own ACs pinning the exact behavior, and this check needs them before
    that ticket lands. Malformed or absent lines simply read as ``None`` --
    never raises."""
    runner = model = None
    for line in spec_text.splitlines():
        if line.startswith("##"):
            break
        m = _RUNNER_FIELD_RE.match(line)
        if not m:
            continue
        field, val = m.group(1), m.group(2).strip()
        if field == "runner":
            runner = val
        elif field == "runner_model":
            model = val
    return runner, model


def check_runner_binary(cfg: Config, now: float, *, which=None) -> dict:
    """WARN (never blocks a spawn) when a current ticket's spec names a
    non-claude ``runner:`` whose CLI binary isn't on ``PATH`` -- OC-2's spawn
    preflight (``dispatcher._default_runner_probe``, the identical
    ``shutil.which`` check, cached once per sweep there) is the actual
    refusal point; this is board-wide visibility only, same shape as
    ``check_ollama_models`` beside it -- one WARN a human can act on before
    it ever surfaces as a spawn skip.

    Skips the check entirely (``ok``, empty ``tickets``) when no current
    ticket names a non-claude runner. Injectable ``which``, as
    ``check_gh_credential_reachability``'s ``run`` kwarg already does, so
    tests never depend on what's actually installed on the machine running
    them."""
    which = which or shutil.which
    home = cfg.home
    candidates: dict[str, str] = {}
    for key in dispatcher.list_keys(home):
        spec_file = store.spec_path(home, key)
        if not spec_file.exists():
            continue
        runner, _model = _spec_runner_fields(spec_file.read_text(encoding="utf-8"))
        if runner and runner != "claude":
            candidates[key] = runner

    if not candidates:
        return {"name": "runner_binary", "status": "ok", "detail": "no ticket uses a non-claude runner",
                "tickets": {}}

    tickets = {}
    for key, runner in candidates.items():
        if which(runner) is None:
            tickets[key] = {"runner": runner, "reason": f"{runner!r} not found on PATH"}
    status = "warn" if tickets else "ok"
    detail = (f"{len(tickets)} ticket(s) name a runner whose binary is missing from PATH"
              if tickets else f"{len(candidates)} ticket(s) using a non-claude runner, all resolve ok")
    return {"name": "runner_binary", "status": status, "detail": detail, "tickets": tickets}


def check_pi_version(cfg: Config, now: float, *, run=None) -> dict:
    """WARN (never blocks a spawn) when the installed ``pi --version`` drifts
    from the pinned ``[runner.pi].version`` (T-56) -- pi has no equivalent of
    OC-2's spawn-time binary probe today (it isn't a registered runner yet,
    ``dispatcher._REGISTERED_RUNNERS``), so this is the only place a version
    drift ever surfaces; same "board-wide visibility, WARN-only" shape as
    ``check_runner_binary``/``check_ollama_models`` beside it.

    Skips entirely (``ok``, ``installed=None``) when ``[runner.pi]`` isn't
    configured or sets no ``version`` -- nothing pinned to drift from.
    Injectable ``run`` (``check_gh_credential_reachability``'s own pattern) so
    tests never shell a real ``pi``.
    """
    run = run or subprocess.run
    pi_table = (cfg.provider_config.get("runner") or {}).get("pi")
    pinned = pi_table.get("version") if isinstance(pi_table, dict) else None
    if not pinned:
        return {"name": "pi_version", "status": "ok",
                "detail": "no [runner.pi].version pinned", "pinned": None, "installed": None}
    try:
        p = run(["pi", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"name": "pi_version", "status": "warn",
                "detail": f"pi --version failed: {type(e).__name__}: {e}",
                "pinned": pinned, "installed": None}
    installed = (p.stdout or "").strip()
    if p.returncode != 0 or not installed:
        return {"name": "pi_version", "status": "warn",
                "detail": (p.stderr or "pi --version failed").strip(),
                "pinned": pinned, "installed": None}
    if installed != pinned:
        return {"name": "pi_version", "status": "warn",
                "detail": f"installed pi {installed} != pinned {pinned}",
                "pinned": pinned, "installed": installed}
    return {"name": "pi_version", "status": "ok",
            "detail": f"installed pi {installed} matches pinned version",
            "pinned": pinned, "installed": installed}


def check_ollama_models(cfg: Config, now: float, *, transport=None) -> dict:
    """WARN (never blocks a spawn) when a current ticket's spec names
    ``runner: opencode`` with a ``runner_model:`` absent from the local
    ollama catalogue, or installed but not tool-capable
    (``ollama.verdict_for_model``) -- OC-2's spawn preflight is the only place a
    bad choice is actually refused; this is board-wide visibility only, one
    sweep ahead of a wedge that would otherwise only surface as a spawn
    failure.

    T-61: opencode-only -- ollama is opencode's OWN backend, not a catalogue
    every non-claude runner shares. A pi ticket names an entirely different
    catalogue (``check_pi_models`` beside it, ``providers/pi.py``); treating
    it as an ollama candidate here would falsely warn on every pi ticket (and
    cost a network call this check's whole "skip when nothing to check"
    contract promises never to spend). Any future non-{claude,opencode,pi}
    runner is likewise not a candidate until it's actually ollama-backed.

    Skips the network call entirely (``ok``, empty ``tickets``) when no
    current ticket names ``runner: opencode`` -- discovery must never cost a
    request on a board with nothing to check. Injectable ``transport``, as
    ``check_gh_credential_reachability``'s ``run`` kwarg already does, so tests
    never hit a real daemon."""
    home = cfg.home
    candidates: list[tuple[str, str]] = []
    for key in dispatcher.list_keys(home):
        spec_file = store.spec_path(home, key)
        if not spec_file.exists():
            continue
        runner, model = _spec_runner_fields(spec_file.read_text(encoding="utf-8"))
        if runner == "opencode" and model:
            candidates.append((key, model))

    if not candidates:
        return {"name": "ollama_models", "status": "ok", "detail": "no ticket uses runner: opencode",
                "tickets": {}}

    models, reason = ollama_mod.fetch_models(transport=transport)
    if models is None:
        return {
            "name": "ollama_models", "status": "warn",
            "detail": f"ollama daemon unreachable ({reason}); "
                      f"{len(candidates)} ticket(s) use runner: opencode",
            "tickets": {key: {"verdict": "unreachable", "model": model, "reason": reason}
                        for key, model in candidates},
        }

    tickets = {}
    for key, model in candidates:
        verdict, vreason = ollama_mod.verdict_for_model(models, reason, model)
        if verdict != "ok":
            tickets[key] = {"verdict": verdict, "model": model, "reason": vreason}
    status = "warn" if tickets else "ok"
    detail = (f"{len(tickets)} ticket(s) name an absent or non-tool-capable ollama model"
              if tickets else f"{len(candidates)} ticket(s) using runner: opencode, all resolve ok")
    return {"name": "ollama_models", "status": status, "detail": detail, "tickets": tickets}


def check_pi_models(cfg: Config, now: float, *, run=None) -> dict:
    """WARN (never blocks a spawn) when a current ticket's spec names
    ``runner: pi`` with a ``runner_model:`` absent from pi's own catalogue
    (``pi.verdict_for_model``) -- OC-2's spawn preflight is the only place a
    bad choice is actually refused; this is board-wide visibility only, same
    shape as ``check_ollama_models`` beside it (T-61: pi's own counterpart,
    now that ``check_ollama_models`` no longer treats a pi ticket as one of
    its own candidates).

    Skips the subprocess probe entirely (``ok``, empty ``tickets``) when no
    current ticket names ``runner: pi`` -- discovery must never cost a
    ``pi --list-models`` shell-out on a board with nothing to check.
    Injectable ``run`` (``check_pi_version``'s own pattern for a pi
    subprocess call) so tests never shell a real ``pi``."""
    home = cfg.home
    candidates: list[tuple[str, str]] = []
    for key in dispatcher.list_keys(home):
        spec_file = store.spec_path(home, key)
        if not spec_file.exists():
            continue
        runner, model = _spec_runner_fields(spec_file.read_text(encoding="utf-8"))
        if runner == "pi" and model:
            candidates.append((key, model))

    if not candidates:
        return {"name": "pi_models", "status": "ok", "detail": "no ticket uses runner: pi",
                "tickets": {}}

    models, reason = pi_mod.fetch_models(store.pi_agent_dir(home), run=run)
    if models is None:
        return {
            "name": "pi_models", "status": "warn",
            "detail": f"pi unreachable ({reason}); {len(candidates)} ticket(s) use runner: pi",
            "tickets": {key: {"verdict": "unreachable", "model": model, "reason": reason}
                        for key, model in candidates},
        }

    tickets = {}
    for key, model in candidates:
        verdict, vreason = pi_mod.verdict_for_model(models, reason, model)
        if verdict != "ok":
            tickets[key] = {"verdict": verdict, "model": model, "reason": vreason}
    status = "warn" if tickets else "ok"
    detail = (f"{len(tickets)} ticket(s) name a pi runner_model absent from pi's catalogue"
              if tickets else f"{len(candidates)} ticket(s) using runner: pi, all resolve ok")
    return {"name": "pi_models", "status": status, "detail": detail, "tickets": tickets}


# MTO-8: consecutive non-429 error outcomes across the fleet's most recent
# terminal sessions before check_provider_availability treats it as a
# provider problem OBSERVED (not a guess off one flaky session).
_PROVIDER_ERROR_STREAK = 3
# Bound on how many of the fleet's most-recent session log FILENAMES are even
# considered -- filename-only, no file opened -- before the (small) prefix of
# those gets tail-scanned for its outcome. Keeps this check's cost independent
# of how long the fleet's full history is.
_PROVIDER_SESSION_SCAN = 12
_PROVIDER_PROBE_HOST = "api.anthropic.com"
_PROVIDER_PROBE_PORT = 443
_PROVIDER_PROBE_TIMEOUT = 5.0

# T-58 (AC7): the confirmation probe's host resolved per runner/provider,
# rather than the single hardcoded Anthropic host above -- a Claude session is
# always Anthropic, but pi is multi-provider by design (its own log records
# which one it actually talked to, see ``steplog._pi_session_outcome``'s
# ``provider`` field). Only the providers pi ships built-in support for (see
# its own ``--provider`` flag) are mapped; anything else (including an
# ollama-backed pi session, which has no meaningful "provider host" to probe --
# it's local) falls back to ``_PROVIDER_PROBE_HOST`` -- a KNOWN, documented
# imprecision (same "if deferred, say so explicitly" call the spec's log-size
# item makes) rather than inventing a probe target for a local model.
_PI_PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "google": "generativelanguage.googleapis.com",
    "openai": "api.openai.com",
}


def _recent_fleet_outcomes(home: Path, keys: list[str], scan: int) -> list[dict]:
    """Newest-first terminal outcomes across EVERY current ticket's session
    logs, capped to the *scan* most-recent session files fleet-wide -- sorted
    by the epoch already embedded in each filename (``sessions.list_sessions``
    with ``with_outcome=False``, so this opens NO log file just to rank them),
    then only the top *scan* get their outcome tail-scanned
    (``steplog.session_outcome``). Returns
    ``[{"outcome": ..., "format": ..., "provider": ...}, ...]`` -- ``format``
    and ``provider`` (T-58) let ``check_provider_availability`` resolve its
    confirmation-probe host per the runner that actually produced the streak,
    instead of assuming Anthropic. This check's PRIMARY signal is
    Anthropic-*or*-pi-specific (see ``_PI_PROVIDER_HOSTS`` above) -- a
    non-Claude, non-pi runner's own log grammar (opencode) is filtered out
    BEFORE the scan window even though ``steplog.session_outcome`` can parse
    it for its own purposes now (OC-5); it is unrelated evidence about any
    resolvable provider host and must never occupy a scan slot or be folded
    into this streak. A ``running`` (still-active claim) entry carries no
    verdict either and is skipped rather than counted or treated as breaking
    a streak.

    T-89 (Gap 3): threads each key's live claim pid through to
    ``steplog.session_outcome`` when the candidate IS that claim's own log
    (``claim["log_path"]`` matches the candidate path exactly) -- the one case
    where a dead pid is actually known -- so a session that crashed before
    writing a single JSON record classifies as ``crashed`` (see that
    function's own docstring) instead of ``running`` forever, and so gets a
    verdict here at all. ``crashed`` counts toward the streak the same as
    ``error`` (below)."""
    candidates: list[dict] = []
    for key in keys:
        for entry in sessions_mod.list_sessions(home, key):
            candidates.append({**entry, "key": key})
    candidates = [c for c in candidates if c["format"] in ("stream-json", "pi")]
    candidates.sort(key=lambda d: d["epoch"], reverse=True)
    claim_by_key = claims.all_claims(home)
    outcomes = []
    for entry in candidates[:scan]:
        claim = claim_by_key.get(entry["key"])
        pid = (claim.get("pid") if claim and claim.get("log_path") == entry["path"]
               else None)
        verdict = steplog.session_outcome(Path(entry["path"]), pid=pid)
        outcome = verdict["outcome"]
        if outcome in ("running", "unknown"):
            continue
        outcomes.append({"outcome": outcome, "format": entry["format"],
                          "provider": verdict.get("provider")})
    return outcomes


def _provider_probe_host(fmt: str | None, provider: str | None) -> str:
    """Resolve the confirmation-probe host (AC7) for the runner/provider that
    produced the tripped error streak. A Claude ``stream-json`` session is
    always Anthropic; a pi session's own log records which provider it
    actually talked to (``_PI_PROVIDER_HOSTS``) -- anything unrecognized (no
    streak yet, or a provider pi supports that this table doesn't) falls back
    to the historical Anthropic default."""
    if fmt == "pi" and provider in _PI_PROVIDER_HOSTS:
        return _PI_PROVIDER_HOSTS[provider]
    return _PROVIDER_PROBE_HOST


def _provider_probe_cache_path(home: Path) -> Path:
    return home / "derived" / ".provider_probe_cache.json"


def _cached_probe(home: Path, host: str, now: float, interval: int, probe) -> tuple[bool, str]:
    """Run *probe* against *host*, reusing the last real result (persisted under
    ``derived/``, since a doctor sweep is a fresh process each time -- an
    in-memory cache would never survive between them) if one was taken against
    the SAME host within *interval* seconds (AC3: bounds the confirmation
    probe's network cost so a frequent doctor sweep / TUI badge refresh can't
    turn this into a probe storm). ``interval <= 0`` disables caching -- every
    call probes fresh, same convention as ``no_output_timeout``/
    ``backup_interval``. A host change always misses the cache (a stale
    Anthropic result must never be served as a Google verdict, or vice versa)."""
    path = _provider_probe_cache_path(home)
    if interval > 0:
        cached = store.read_json(path, {}) or {}
        ts = cached.get("ts")
        if (cached.get("host") == host and isinstance(ts, (int, float))
                and 0 <= now - ts < interval):
            return bool(cached.get("reachable")), str(cached.get("reason", ""))
    reachable, reason = probe(host)
    if interval > 0:
        store.write_json(path, {"host": host, "ts": now, "reachable": reachable, "reason": reason})
    return reachable, reason


def _default_provider_probe(host: str = _PROVIDER_PROBE_HOST) -> tuple[bool, str]:
    """Real confirmation probe -- a bare TCP connect to *host* (AC7: resolved
    per runner/provider by ``_provider_probe_host``, defaulting to the
    historical Anthropic host), no TLS handshake, no request, no API key
    spent. Cheap enough to run as a confirmation step, but
    ``check_provider_availability`` only ever reaches this once the
    session-log primary signal has already tripped -- never on a bare sweep
    with nothing wrong, so this can't turn the health check into a traffic
    generator. A successful connect means "this box has a network path to the
    provider"; it says nothing about whether the provider itself is erroring
    -- that distinction is exactly what the already-tripped session-log
    streak answered."""
    import socket
    try:
        with socket.create_connection(
            (host, _PROVIDER_PROBE_PORT), timeout=_PROVIDER_PROBE_TIMEOUT
        ):
            return True, f"TCP connect to {host}:{_PROVIDER_PROBE_PORT} ok"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def check_provider_availability(cfg: Config, now: float, *, probe=None) -> dict:
    """Report-only (MTO-8; this ticket's Notes call for that explicitly --
    never blocks a spawn, so a broken detector can't halt the board), and
    distinguishes the three states the operator actually needs to tell apart:
    provider fine, provider reachable but erroring, or this box has no
    network at all. None of those are visible today above the level of one
    session's own log (``steplog.classify_result``).

    PRIMARY signal, no network call: the fleet's most recent terminal session
    outcomes (``_recent_fleet_outcomes``, bounded scan) -- if the most recent
    ``_PROVIDER_ERROR_STREAK`` of them are ALL ``error``/``crashed`` (T-89
    Gap 3: a dead-pid session whose log never wrote a single JSON record
    counts the same as a parsed error result), this reports a provider
    problem OBSERVED, not probed. A ``rate_limited`` (429) outcome breaks the
    streak rather than counting toward it -- rate limiting is already handled
    by ``ratelimit.py``'s own gate and must never be conflated with
    unreachability (the operator's next action differs: wait vs. investigate).
    Fewer than the threshold with at least SOME session history in the
    scanned window: ``ok``, no probe.

    T-89 (Gap 2, AC3): a board with ZERO terminal session outcomes at all
    (``outcomes`` empty -- a fresh home, or one whose recent claims are all
    still ``running``/unrecognized) used to fail open here too, returning
    ``ok`` with a "provider reachable" detail it never measured. That is
    exactly the silent fail-open this ticket's Notes call out -- a bare
    ``ok`` should never claim reachability nothing observed. Instead this now
    runs the SAME confirmation probe (cost-bounded by ``_cached_probe`` /
    ``cfg.provider_probe_interval_s``, so a frequent sweep on an idle board
    can't turn into a probe storm) against the historical Anthropic default
    host (no streak evidence exists yet to resolve a runner-specific one) and
    reports ``ok`` (probe reached it) or ``no_network`` (probe failed) --
    either way an HONEST, measured verdict, with ``error_streak: 0`` and a
    detail that never contains "provider reachable" on the failing branch.

    CONFIRMATION probe, reached once the primary signal has tripped OR the
    board has no history at all: a bare TCP connect to a host resolved per
    runner/provider (AC7: ``_provider_probe_host`` -- Anthropic for a Claude
    streak, the erroring pi sessions' own recorded provider for a pi streak,
    see ``_PI_PROVIDER_HOSTS``) via ``_default_provider_probe``. Connects ->
    ``erroring`` (network is up, the provider itself is degraded) when a
    streak tripped, ``ok`` when there was no streak to confirm; fails ->
    ``no_network`` either way (this box is offline). Injectable ``probe``
    (now called with that resolved host, same shape as
    ``check_gh_credential_reachability``'s ``run``/``check_ollama_models``'s
    ``transport``, so tests never open a real socket).
    """
    home = cfg.home
    keys = dispatcher.list_keys(home)
    outcomes = _recent_fleet_outcomes(home, keys, _PROVIDER_SESSION_SCAN)
    streak = 0
    streak_fmt: str | None = None
    streak_provider: str | None = None
    for entry in outcomes:
        if entry["outcome"] not in ("error", "crashed"):
            break
        if streak == 0:
            streak_fmt, streak_provider = entry["format"], entry["provider"]
        streak += 1
        if streak >= _PROVIDER_ERROR_STREAK:
            break
    if streak < _PROVIDER_ERROR_STREAK:
        if outcomes:
            return {"name": "provider_availability", "status": "ok", "state": "ok",
                    "detail": "provider reachable, no recent error streak",
                    "error_streak": streak}
        # AC3: no terminal session outcomes to measure from at all -- probe
        # rather than assert reachability nothing observed.
        reachable, reason = _cached_probe(
            home, _PROVIDER_PROBE_HOST, now, cfg.provider_probe_interval_s,
            probe or _default_provider_probe)
        if reachable:
            return {"name": "provider_availability", "status": "ok", "state": "ok",
                    "detail": f"no session history to measure from; network probe reachable ({reason})",
                    "error_streak": 0}
        return {"name": "provider_availability", "status": "fail", "state": "no_network",
                "detail": (f"no session history to measure from; network probe unreachable "
                           f"({reason}) -- this machine appears offline"),
                "error_streak": 0}

    host = _provider_probe_host(streak_fmt, streak_provider)
    reachable, reason = _cached_probe(
        home, host, now, cfg.provider_probe_interval_s, probe or _default_provider_probe)
    if reachable:
        return {
            "name": "provider_availability", "status": "warn", "state": "erroring",
            "detail": (f"{streak} consecutive session(s) ended in a provider error; "
                       f"network reachable ({reason}) -- the provider itself appears degraded"),
            "error_streak": streak,
        }
    return {
        "name": "provider_availability", "status": "fail", "state": "no_network",
        "detail": (f"{streak} consecutive session(s) ended in a provider error; "
                   f"network unreachable ({reason}) -- this machine appears offline"),
        "error_streak": streak,
    }


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


def check_burn(cfg: Config, now: float) -> dict:
    """RB-11: per-key burn visibility. WARN when any key is flagged by either
    of ``burn.report``'s two signals -- byte-identical ``Failed`` text (the
    measured T-55/T-56 shape) or spawns piling up with ``observed_seq``
    frozen (catches a free runner too, spec AC3 -- neither signal depends on
    spend). Never trips on a key making real progress, however many spawns it
    has consumed (see ``burn.py``'s module docstring for why). Lazy import:
    ``burn`` imports this module at its own top level (for ``WINDOW_SECONDS``/
    ``spawn_rate``), so importing it back here at module load time would be a
    load-time cycle."""
    from . import burn

    rpt = burn.report(cfg, now)
    flagged = rpt["flagged"]
    status = "warn" if flagged else "ok"
    detail = (f"{len(flagged)} key(s) burning: {', '.join(flagged)}" if flagged
              else "no key burning (repeated identical failures, or spawns with no progress)")
    return {"name": "burn", "status": status, "detail": detail, **rpt}


def check_worktree_health(cfg: Config, now: float) -> dict:
    """WARN (detective, never blocks a spawn) on any EXISTING worktree dir
    under ``<home>/worktrees`` that fails `ops.worktree_health` -- no index, or
    a mass-deletion `git status`. MTO-1: this exact shape was invisible until a
    human ran `git status` inside the worktree by hand; a fresh `worktree
    ensure` on the same key now only self-heals it when its creation genuinely
    never completed (T-81: a completed worktree that later trips this
    heuristic is left alone, not torn down), so this check is what surfaces it
    at all. Lazy `ops` import -- `ops` imports `dispatcher`, which lazily
    imports `health` itself; importing `ops` at module level here would
    round-trip that cycle at import time.

    `ops.worktree_health` now runs its probes under `cfg.worktree_timeout`
    (T-81 AC3) and can raise `store.MaestroError` if a probe itself times out
    or errors -- that is NOT the same as "unhealthy" (AC5), so this loop
    catches it per-worktree and reports it as its own broken entry rather
    than letting one slow probe crash the rest of `maestro doctor`."""
    from . import ops

    worktrees_dir = cfg.home / "worktrees"
    broken = []
    if worktrees_dir.is_dir():
        for wt in sorted(p for p in worktrees_dir.iterdir() if p.is_dir()):
            try:
                health = ops.worktree_health(wt, timeout=cfg.worktree_timeout)
            except store.MaestroError as exc:
                broken.append({"key": wt.name, "reason": f"health probe error: {exc}"})
                continue
            if not health["healthy"]:
                broken.append({"key": wt.name, "reason": health["reason"]})
    status = "warn" if broken else "ok"
    detail = (", ".join(f"{b['key']}: {b['reason']}" for b in broken)
              if broken else "all existing worktrees have a valid index and clean status")
    return {"name": "worktree_health", "status": status, "detail": detail, "broken": broken}


# The check registry: cmd_doctor/report() run every entry and surface the
# results under "checks", in addition to the existing top-level fields kept
# for backward compatibility with the TUI fleet view and prior doctor output.
# Every entry is called uniformly as (cfg, now, **kw) -- run_checks below
# iterates this tuple directly rather than slicing it, so prepending,
# appending, or reordering an entry here is enough to include it; only
# check_heartbeat's plist override is special-cased, by identity, since it's
# the one check with a caller-supplied kwarg to thread through.
CHECKS = (check_heartbeat, check_backup_age, check_claim_age, check_claim_no_output,
          check_dead_letters, check_phantom_keys, check_missing_acs, check_watchdog_loops,
          check_depends_on, check_repo_preflight, check_unknown_repo_bindings, check_missing_reconcile_skill,
          check_reconciler_permissions, check_spawn_floor, check_daily_spend, check_burn,
          check_gh_credential_reachability, check_launchctl, check_ollama_models, check_pi_models,
          check_runner_binary, check_pi_version, check_worktree_health, check_provider_availability)


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
    burn_check = next(c for c in checks if c["name"] == "burn")
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
        # RB-11: per-key spend over the SAME trailing window `spawns_last_hour.by_key`
        # already covers the spawn side of -- together they're the "spawns and spend
        # over a recent window, per key" this ticket's Intent asked for, without a new
        # dashboard. `burning_keys` is the union of burn.py's two flag signals (see
        # check_burn); never populated for a key genuinely making progress.
        "spend_usd_by_key": burn_check["spend_usd_by_key"],
        "burning_keys": burn_check["flagged"],
        "paused": hb.get("paused", False),
        "checks": checks,
    }
