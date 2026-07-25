"""The dispatcher — a level-triggered, non-blocking work queue.

This is pure Python (no LLM). A launchd ``StartInterval`` fires ``maestro dispatch``
every few minutes; each invocation:

  1. mints tickets from the ``_new`` inbox,
  2. sweeps every ticket's *snapshot* (cheap — never the full log),
  3. computes which are DUE (spec changed, inbox pending, requeue timer elapsed,
     or simply in an active phase),
  4. skips keys already claimed by a live session (per-key serialization),
  5. spawns one reconciler per remaining due key, up to ``max_concurrency``,
  6. exits immediately — it NEVER blocks on a ralph-loop.

No global lock, no wave barrier. Different keys are wholly independent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import events as E
from . import event_log, inbox, notify, schedule, snapshot as snap_mod, store
from .config import Config
from .idempotency import content_hash
from .sessions import SessionManager
from .statemachine import Phase, SLEEPING_PHASES, TERMINAL_PHASES


@dataclass
class DueResult:
    due: bool
    reason: str


def spec_hash_on_disk(home: Path, key: str) -> str | None:
    path = store.spec_path(home, key)
    if not path.exists():
        return None
    return content_hash(path.read_text(encoding="utf-8"))


_DEPENDS_ON_RE = re.compile(r"^\s*dependsOn\s*:\s*\[([^\]]*)\]", re.MULTILINE)
_KEY_RE = re.compile(r"^([A-Za-z]+)-(\d+)$")
_FRONTMATTER_FIELD_RE = re.compile(r"^([a-zA-Z_]\w*)\s*:\s*(.+)$")


def split_key(key: str) -> tuple:
    """Natural sort key: (0, prefix, number) for well-formed keys, (1, key, 0) for others."""
    m = _KEY_RE.match(key)
    if m:
        return (0, m.group(1), int(m.group(2)))
    return (1, key, 0)


def parse_depends_on(spec_text: str) -> list[str]:
    """Extract the dependsOn list from a spec's loose frontmatter."""
    m = _DEPENDS_ON_RE.search(spec_text)
    if not m:
        return []
    raw = m.group(1)
    return [k.strip() for k in raw.split(",") if k.strip()]


def parse_spec_overrides(spec_text: str) -> dict:
    """Extract optional kind/model/effort from a spec's loose frontmatter.

    Stops at the first ## section header. Returns only keys that are present.
    """
    result: dict = {}
    for line in spec_text.splitlines():
        if line.startswith("##"):
            break
        m = _FRONTMATTER_FIELD_RE.match(line)
        if not m:
            continue
        field, val = m.group(1), m.group(2).strip()
        if field in ("kind", "model", "effort"):
            result[field] = val
    return result


def is_due(snap: snap_mod.Snapshot, *, inbox_pending: bool,
           current_spec_hash: str | None, now: float,
           blocked_dep: bool = False) -> DueResult:
    phase = Phase(snap.phase)
    if phase in TERMINAL_PHASES:
        return DueResult(False, "terminal")
    if inbox_pending:
        return DueResult(True, "inbox")
    # A reconciler that crashed between fold-inbox and set-phase leaves answered_questions
    # populated but inbox already acked. Wake the ticket so it can finish the transition.
    if phase == Phase.AWAITING_HUMAN and snap.answered_questions:
        return DueResult(True, "answered-pending")
    # Safety net: awaiting-human with no open question, no pending answer, and no timer
    # has nothing left to wait on — it is stranded (e.g. a direct set-phase that never
    # asked). Wake it so the reconciler can recover instead of sleeping forever.
    if phase == Phase.AWAITING_HUMAN and not snap.open_questions and not snap.answered_questions:
        return DueResult(True, "stranded")
    if current_spec_hash is not None and current_spec_hash != snap.spec_hash:
        return DueResult(True, "spec-changed")
    if phase == Phase.READY and blocked_dep:
        return DueResult(False, "blocked-dep")
    # A pending requeue timer holds the ticket in EVERY non-terminal phase, not only
    # the sleeping ones. This check used to live inside the SLEEPING_PHASES branch,
    # so a reconciler that asked to sleep from an *active* phase (`maestro requeue`,
    # which is exactly what the in-review handler does) was ignored and the ticket
    # came back due on the very next sweep — the 2026-07-19 runaway, 21,731 no-op
    # sessions with 5,522 discarded RequeueScheduled events behind them.
    if snap.next_requeue_at is not None:
        if snap.next_requeue_at > now:
            return DueResult(False, "backoff")
        return DueResult(True, "timer")
    # No timer pending. A sleeping phase waits for a signal; anything else has work.
    # NOTE: in-review deliberately stays active-when-untimed — nothing else on main
    # polls a PR, so a null timer must not strand the ticket forever. The spawn-rate
    # floor in dispatch() is what bounds it.
    if phase in SLEEPING_PHASES:
        return DueResult(False, "sleeping")
    return DueResult(True, "active")


def list_keys(home: Path) -> list[str]:
    """All known ticket keys (union of ticket dirs, event logs, snapshots),
    excluding archived/dead-letter."""
    keys: set[str] = set()
    tickets = home / "tickets"
    if tickets.exists():
        for d in tickets.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                keys.add(d.name)
    for sub, suffix in ((home / "events", ".jsonl"),
                        (home / "derived" / "snapshots", ".json")):
        if sub.exists():
            for f in sub.iterdir():
                if f.is_file() and f.name.endswith(suffix) and not f.name.startswith("."):
                    name = f.name[: -len(suffix)]
                    if name.endswith(".archive"):
                        continue
                    keys.add(name)
    return sorted(keys, key=split_key)


def existing_prefixes(home: Path) -> list[str]:
    """Return sorted unique prefixes from all existing well-formed ticket keys."""
    seen: set[str] = set()
    for key in list_keys(home):
        m = _KEY_RE.match(key)
        if m:
            seen.add(m.group(1))
    return sorted(seen)


def mint_new_tickets(cfg: Config) -> list[str]:
    """Drain the keyless ``_new`` inbox into real ticket dirs + TicketCreated events.

    A create-request may carry a period-bucket ``dedup`` token (scheduled tasks do —
    see ``run_scheduled_tasks``) to close the window between that tick's ``_new``
    append and its cursor write: if a crash there causes the same period to be
    re-fired, the re-fired request carries the same token and is skipped here,
    making the re-fire a guaranteed no-op instead of a duplicate ticket.
    """
    home = cfg.home
    minted: list[str] = []
    minted_path = home / "derived" / ".schedule_minted.json"
    minted_dedup = store.read_json(minted_path, {}) or {}
    dedup_changed = False
    for _idx, entry in inbox.pending_new(home):
        ticket_args = entry.get("args") or {}
        dedup = ticket_args.get("dedup")
        if dedup and dedup in minted_dedup:
            continue  # already minted this period — guaranteed no-op
        prefix = entry.get("prefix") or None
        key = entry.get("key") or _auto_key(home, prefix=prefix or "T")
        try:
            store.validate_key(key)
        except store.MaestroError:
            continue
        if event_log.last_seq(home, key) > 0:
            # Key already has events -- it was triaged (or otherwise advanced)
            # before this mint sweep drained the matching inbox/_new entry.
            # Appending another TicketCreated here would clobber snapshot.fold's
            # phase back to triaging, silently orphaning any progress since.
            # Treat the request as a no-op: the ticket already exists.
            if dedup:
                minted_dedup[dedup] = key
                dedup_changed = True
            continue
        spec = store.spec_path(home, key)
        title = entry.get("title") or key
        if not spec.exists():
            store.atomic_write(spec, _seed_spec(key, title, ticket_args))
        ticket_payload: dict = {
            "title": title,
            "source": "inbox/_new",
            "spec_hash": spec_hash_on_disk(home, key),
        }
        if ticket_args.get("kind"):
            ticket_payload["kind"] = ticket_args["kind"]
        if ticket_args.get("external_source"):
            ticket_payload["external_source"] = ticket_args["external_source"]
        if ticket_args.get("external_id"):
            ticket_payload["external_id"] = ticket_args["external_id"]
        if ticket_args.get("scheduled_by"):
            ticket_payload["scheduled_by"] = ticket_args["scheduled_by"]
        event_log.append(
            home, key, E.TICKET_CREATED,
            ticket_payload,
            actor="dispatcher",
            step_id=f"create-{key}",
        )
        snap_mod.rebuild(home, key)
        minted.append(key)
        if dedup:
            minted_dedup[dedup] = key
            dedup_changed = True
    inbox.ack_new(home)
    if dedup_changed:
        store.write_json(minted_path, minted_dedup)
    return minted


def _auto_key(home: Path, prefix: str = "T") -> str:
    n = 1
    while (home / "tickets" / f"{prefix}-{n}").exists():
        n += 1
    return f"{prefix}-{n}"


def _seed_spec(key: str, title: str, args: dict) -> str:
    # A create-request may carry explicit ``null`` for any field (JSON), so ``dict.get``
    # with a default is not enough — coerce None to the fallback for every field that
    # is rendered into the spec, or a null intent/title/deps would crash the sweep.
    title = title or key
    tier = args.get("approval_tier") or 1
    priority = args.get("priority") or 3
    depends_on = args.get("depends_on") or []
    deps_str = ", ".join(depends_on) if depends_on else ""
    lines = [
        f"# {key}: {title}",
        "",
        "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->",
        "",
        f"approval_tier: {tier}",
        f"priority: {priority}",
    ]
    if args.get("kind"):
        lines.append(f"kind: {args['kind']}")
    if args.get("model"):
        lines.append(f"model: {args['model']}")
    if args.get("effort"):
        lines.append(f"effort: {args['effort']}")
    lines.append(f"dependsOn: [{deps_str}]")
    lines.append("")
    lines.append("## Intent")
    lines.append(args.get("intent") or "(describe what done looks like)")
    if args.get("notes"):
        lines.append("")
        lines.append("## Notes")
        lines.append(args["notes"])
    lines.append("")
    lines.append("## Acceptance criteria")
    lines.append("- ")
    return "\n".join(lines) + "\n"


def sync_external_sources(cfg: Config, now: float) -> dict:
    """Opt-in external-tracker sync tick (e.g. Jira). No-op unless a tracker other
    than "none" is configured, and gated to run at most once per that provider's
    ``sync_interval`` seconds via a persisted cursor (level-triggered, idempotent —
    matches ``mint_new_tickets``). Imports new work via ``import_new`` (which itself
    funnels through the audited ``_new`` inbox) and refreshes every tracked,
    not-done ticket sourced from that tracker.
    """
    home = cfg.home
    tracker_name = cfg.providers.get("tracker", "none")
    if tracker_name in (None, "", "none"):
        return {"imported": 0, "refreshed": 0}

    settings = cfg.provider_config.get("tracker", {}).get(tracker_name, {})
    interval = int(settings.get("sync_interval", 900))
    cursor_path = home / "derived" / ".sync_cursor.json"
    cursor = store.read_json(cursor_path, {}) or {}
    last_sync = cursor.get(tracker_name, 0)
    if now - last_sync < interval:
        return {"imported": 0, "refreshed": 0}

    # Import lazily to avoid a hard dependency from the core onto any one adapter.
    from . import providers

    tracker = providers.get_tracker(cfg)
    imported = tracker.import_new(home)

    refreshed = 0
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        if snap.external_source != tracker_name:
            continue
        if Phase(snap.phase) in TERMINAL_PHASES:
            continue
        if not snap.external_id:
            continue
        refreshed += tracker.refresh(home, key, snap.external_id)
        if event_log.last_seq(home, key) > snap.observed_seq:
            snap_mod.rebuild(home, key)

    cursor[tracker_name] = now
    store.write_json(cursor_path, cursor)
    return {"imported": imported, "refreshed": refreshed}


def _schedule_cursor_path(home: Path) -> Path:
    return home / "derived" / ".schedule_cursor.json"


def run_scheduled_tasks(cfg: Config, now: float) -> dict:
    """Config-declared recurring-trigger tick, mirroring ``sync_external_sources``:
    for each enabled+due ``[[scheduled]]`` task, mint one create-request into the
    ``_new`` inbox and advance a single ``derived/.schedule_cursor.json``
    ({name: last_fired_ts}). Level-triggered, not edge-accumulating: a task fires
    ONCE on the next sweep after a long downtime, resetting its cursor to ``now``
    rather than catching up.
    """
    home = cfg.home
    cursor_path = _schedule_cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    fired: list[str] = []
    for task in cfg.scheduled:
        if not task.get("enabled", True):
            continue
        name = task.get("name")
        if not name or not task.get("prompt") or not task.get("every"):
            continue
        last = cursor.get(name, 0)
        if not schedule.is_due(task, last, now):
            continue
        bucket = int(now // schedule.period(task))
        inbox.append_new(
            home,
            title=task.get("title") or name,
            prefix=task.get("prefix"),
            args={
                "intent": task["prompt"],
                "kind": task.get("kind", "implementation"),
                "approval_tier": task.get("approval_tier", 1),
                "priority": task.get("priority", 3),
                "scheduled_by": name,
                "dedup": f"{name}:{bucket}",
            },
        )
        cursor[name] = now
        fired.append(name)
    if fired:
        store.write_json(cursor_path, cursor)
    return {"fired": fired}


def schedule_status(cfg: Config, now: float) -> list[dict]:
    """Read-only view of every configured task's cadence + cursor state, for
    ``maestro schedule list`` and the TUI schedule panel."""
    cursor = store.read_json(_schedule_cursor_path(cfg.home), {}) or {}
    rows: list[dict] = []
    for task in cfg.scheduled:
        name = task.get("name")
        last = cursor.get(name, 0) or None
        enabled = task.get("enabled", True)
        rows.append({
            "name": name,
            "prompt": task.get("prompt"),
            "every": task.get("every"),
            "kind": task.get("kind", "implementation"),
            "approval_tier": task.get("approval_tier", 1),
            "priority": task.get("priority", 3),
            "prefix": task.get("prefix"),
            "enabled": enabled,
            "last_fired": last,
            "next_due": schedule.next_due(task, last or 0) if enabled and task.get("every") else None,
        })
    return rows


def sync_worktrees(cfg: Config) -> dict:
    """Keep other tickets' worktrees from drifting behind a just-merged main.

    Refreshes ``origin/main`` in the primary repo (fast-forwarding the local
    ``main`` branch there too, when that's the checked-out branch), then for
    every ticket sitting in ``awaiting-ci``/``in-review`` with a worktree that
    is now behind, routes it back into ``implementing`` so the reconciler
    rebases and resolves any conflict exactly as it already does for a
    GitHub-reported CONFLICTING PR (see ``ops.route_conflict``). A ticket that's
    already ``implementing`` needs no nudge — it re-syncs with ``origin/main``
    on every turn on its own. Level-triggered and idempotent: no-op when nothing
    is behind, and a no-op repo/network problem never raises.
    """
    import subprocess

    from . import ops

    home = cfg.home
    repo = cfg.repo_path
    routed: list[str] = []
    if not repo or not Path(repo).exists():
        return {"fetched": False, "routed": routed}

    fetch = subprocess.run(["git", "-C", repo, "fetch", "-q", "origin", "main"],
                           capture_output=True, text=True)
    if fetch.returncode != 0:
        return {"fetched": False, "routed": routed}
    subprocess.run(["git", "-C", repo, "merge", "-q", "--ff-only", "origin/main"],
                   capture_output=True, text=True)  # best-effort; no-op if main isn't checked out here

    for key in list_keys(home):
        wt = home / "worktrees" / key
        if not wt.exists():
            continue
        snap = snap_mod.load(home, key)
        if Phase(snap.phase) not in (Phase.AWAITING_CI, Phase.IN_REVIEW):
            continue
        behind = subprocess.run(
            ["git", "-C", str(wt), "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True,
        )
        if behind.returncode != 0:
            continue
        try:
            count = int((behind.stdout or "0").strip())
        except ValueError:
            count = 0
        if count == 0:
            continue
        if ops.route_stale(cfg, key):
            routed.append(key)

    return {"fetched": True, "routed": routed}


def _vcs_cursor_path(home: Path) -> Path:
    return home / "derived" / ".vcs_cursor.json"


def sync_vcs(cfg: Config, now: float) -> dict:
    """Cursor-gated PR-observation tick, patterned on ``sync_external_sources``:
    for every awaiting-ci / in-review ticket carrying a ``pr_number``, polls PR
    state, CI checks, and review comments via ``get_vcs(cfg)`` (opt-in — a no-op
    unless a vcs provider other than "none" is configured) and advances the
    ticket directly, so neither phase needs a reconciler spawn to make progress:

    - merged -> ``ops.check_merged`` (finalizes) + the worktree is removed
    - CONFLICTING -> ``ops.route_conflict`` (rebase/resolve in implementing)
    - failing CI -> implementing, with the failing check names in the reason
    - passing CI (from awaiting-ci) -> in-review
    - a CHANGES_REQUESTED review -> implementing, with the verbatim comment body

    Replaces the reconciler's own ``gh pr checks`` shelling: CI/PR/review
    observation is now dispatcher-owned, pure-Python, and idempotent per
    PR-head-SHA/check-set (CI) or comment-id (reviews).
    """
    home = cfg.home
    vcs_name = cfg.providers.get("vcs", "none")
    if vcs_name in (None, "", "none"):
        return {"checked": 0}

    settings = cfg.provider_config.get("vcs", {}).get(vcs_name, {})
    interval = int(settings.get("sync_interval", 120))
    cursor_path = _vcs_cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    last_sync = cursor.get(vcs_name, 0)
    if now - last_sync < interval:
        return {"checked": 0}

    from . import providers  # lazy: avoid a hard import-time dependency

    vcs = providers.get_vcs(cfg)
    checked = 0
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        phase = Phase(snap.phase)
        if phase not in (Phase.AWAITING_CI, Phase.IN_REVIEW) or not snap.pr_number:
            continue
        checked += 1
        status = vcs.pr_status(snap.pr_number)

        if _route_if_merged(cfg, key, status):
            continue
        if status.get("mergeable") == "CONFLICTING":
            from . import ops
            ops.route_conflict(cfg, key, snap.pr_number, actor="dispatcher")
            continue

        _observe_ci(cfg, key, status, phase)
        _observe_reviews(cfg, key, snap.pr_number, vcs)

    cursor[vcs_name] = now
    store.write_json(cursor_path, cursor)
    return {"checked": checked}


def _route_if_merged(cfg: Config, key: str, status: dict) -> bool:
    from . import ops
    if not ops.check_merged(cfg, key, status.get("state", ""), actor="dispatcher"):
        return False
    import subprocess
    wt = cfg.home / "worktrees" / key
    if wt.exists() and cfg.repo_path:
        subprocess.run(["git", "-C", cfg.repo_path, "worktree", "remove", str(wt), "--force"],
                       capture_output=True, text=True)
    return True


def _observe_ci(cfg: Config, key: str, status: dict, phase: Phase) -> None:
    ci_state = status.get("ci_state", "unknown")
    failing = sorted(status.get("failing_checks") or [])
    head_sha = status.get("head_sha") or "unknown"
    check_key = content_hash(ci_state + ":" + ",".join(failing))
    sid = f"ci-{key}-{head_sha}-{check_key}"
    detail = f"{len(failing)} check(s) failing: {', '.join(failing)}" if failing else ""
    ev = event_log.append(cfg.home, key, E.CI_OBSERVED,
                          {"state": ci_state, "failing_checks": failing, "detail": detail},
                          actor="dispatcher", step_id=sid)
    if ev is None:
        return  # unchanged since the last poll — nothing to route
    snap_mod.rebuild(cfg.home, key)
    from . import ops
    if ci_state == "failing":
        ops.set_phase(cfg, key, Phase.IMPLEMENTING,
                      reason=f"CI failing: {', '.join(failing)}", actor="dispatcher")
    elif ci_state == "passing" and phase == Phase.AWAITING_CI:
        ops.set_phase(cfg, key, Phase.IN_REVIEW, reason="CI passing", actor="dispatcher")


def _observe_reviews(cfg: Config, key: str, pr_number: int, vcs) -> None:
    changes_requested_body: str | None = None
    for r in vcs.review_feedback(pr_number):
        cid = r.get("id")
        if not cid:
            continue
        ev = event_log.append(
            cfg.home, key, E.REVIEW_FEEDBACK_RECEIVED,
            {"comment_id": cid, "state": r.get("state"), "body": r.get("body", ""),
             "author": r.get("author")},
            actor="dispatcher", step_id=f"review-{key}-{cid}",
        )
        if ev is not None and r.get("state") == "CHANGES_REQUESTED":
            changes_requested_body = r.get("body", "")
    if changes_requested_body is None:
        return
    snap_mod.rebuild(cfg.home, key)
    from . import ops
    ops.set_phase(cfg, key, Phase.IMPLEMENTING,
                 reason=f"changes requested: {changes_requested_body}", actor="dispatcher")


def _has_unmet_deps(home: Path, key: str) -> bool:
    """Return True if any dependsOn entry for *key* is not yet done."""
    spec_file = store.spec_path(home, key)
    if not spec_file.exists():
        return False
    deps = parse_depends_on(spec_file.read_text(encoding="utf-8"))
    for dep in deps:
        dep_snap = snap_mod.load(home, dep)
        if Phase(dep_snap.phase) not in TERMINAL_PHASES:
            return True
    return False


@dataclass
class DispatchReport:
    minted: list[str]
    due: list[tuple[str, str]]      # (key, reason)
    claimed: list[str]              # due but a live session already owns it
    spawned: list[str]
    capacity_skipped: list[str]     # due + free but over the concurrency cap
    active_sessions: int
    scheduled_fired: list[str] = field(default_factory=list)
    throttled: list[str] = field(default_factory=list)  # due + free but under the spawn floor


# Due-reasons that represent a HUMAN acting right now. These bypass the spawn-rate
# floor: a person who answers a question or edits a spec must get an immediate
# reconcile, and their own hands are the rate limit. Everything else is throttled.
_UNTHROTTLED_REASONS = frozenset({"inbox", "spec-changed", "answered-pending"})


def _spawn_ledger_path(home: Path) -> Path:
    return home / "derived" / ".spawn_ledger.json"


# Hard cap on how many timestamps `recent` may hold per key, independent of the
# window filter -- a board with the spawn floor disabled fires many spawns within
# the same second, and the window filter alone does not bound growth when
# `now` barely advances between writes. One entry per window-second is already
# far more resolution than an hourly rate needs.
_LEDGER_RECENT_CAP = 3600  # == health.WINDOW_SECONDS; kept as a literal, health imports us


def spawn_floor(cfg: Config) -> int:
    """Minimum seconds between two spawns of the same key (0 disables)."""
    floor = cfg.min_spawn_interval
    if floor is None:
        floor = cfg.reconcile_steady_interval
    return max(0, int(floor))


def dispatch(cfg: Config, sessions: SessionManager, now: float) -> DispatchReport:
    """One sweep. ``sessions`` decides whether spawns actually launch (use
    ``DryRunSessions`` to record-without-launch). Always idempotent and safe to
    run on a timer — minting and folding are no-ops when nothing changed.
    """
    home = cfg.home
    from . import backup  # lazy: backup -> projection -> dispatcher would cycle at import time
    from . import health  # lazy: health -> dispatcher would cycle at import time

    minted = mint_new_tickets(cfg)
    sync_external_sources(cfg, now)
    scheduled_fired = run_scheduled_tasks(cfg, now)["fired"]
    sync_worktrees(cfg)
    sync_vcs(cfg, now)
    backup.maybe_backup(cfg, now)
    notify.maybe_notify(cfg, now)

    active = sessions.list_active()
    due: list[tuple[str, str]] = []
    claimed: list[str] = []

    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        # Keep the dispatcher's view fresh if a writer fell behind on its fold.
        if event_log.last_seq(home, key) > snap.observed_seq:
            snap = snap_mod.rebuild(home, key)
        blocked_dep = _has_unmet_deps(home, key)
        res = is_due(
            snap,
            inbox_pending=inbox.has_pending(home, key),
            current_spec_hash=spec_hash_on_disk(home, key),
            now=now,
            blocked_dep=blocked_dep,
        )
        if not res.due:
            continue
        if key in active:
            claimed.append(key)        # per-key serialization: one reconciler per key
            continue
        due.append((key, res.reason))

    # Spawn-rate floor. The claim file is the ONLY other per-key spawn memory and it
    # is unlinked the moment the worker dies — so a session that exits in under a
    # second (a rate-limit rejection, a crash) leaves nothing behind and the key is
    # instantly re-spawnable. This ledger outlives the process and bounds the rate no
    # matter how often the dispatcher itself is fired.
    floor = spawn_floor(cfg)
    ledger_path = _spawn_ledger_path(home)
    ledger = store.read_json(ledger_path, {}) or {}
    throttled: list[str] = []
    if floor:
        eligible: list[tuple[str, str]] = []
        for key, reason in due:
            entry = ledger.get(key)
            last = entry.get("last") if isinstance(entry, dict) else entry
            if (reason not in _UNTHROTTLED_REASONS
                    and isinstance(last, (int, float)) and now - last < floor):
                throttled.append(key)
                continue
            eligible.append((key, reason))
    else:
        eligible = due

    slots = max(0, cfg.max_concurrency - len(active))
    to_spawn = eligible[:slots]
    capacity_skipped = [k for k, _ in eligible[slots:]]

    spawned: list[str] = []
    for key, _reason in to_spawn:
        cwd = _worker_cwd(cfg, key)
        prompt = f"{cfg.reconcile_command} {key}"
        model, effort = _resolve_model_effort(cfg, key)
        sessions.spawn(key, prompt, cwd, model=model, effort=effort)
        spawned.append(key)
        prev = ledger.get(key)
        recent = list(prev.get("recent", [])) if isinstance(prev, dict) else []
        recent.append(now)
        recent = [t for t in recent if now - t <= health.WINDOW_SECONDS][-_LEDGER_RECENT_CAP:]
        ledger[key] = {"last": now, "recent": recent}

    if spawned:
        # Keep the ledger from growing without bound as keys come and go.
        known = set(list_keys(home))
        store.write_json(ledger_path,
                         {k: v for k, v in ledger.items() if k in known})

    _write_heartbeat(home, now, len(spawned), len(active), len(throttled), len(due))
    return DispatchReport(
        minted=minted, due=due, claimed=claimed, spawned=spawned,
        capacity_skipped=capacity_skipped, active_sessions=len(active),
        scheduled_fired=scheduled_fired, throttled=throttled,
    )


def _resolve_model_effort(cfg: Config, key: str) -> tuple[str, str | None]:
    """Resolve the model and effort for spawning *key*'s reconciler.

    Precedence: spec front-matter → kind default (research) → config defaults.
    Returns (model, effort) where effort may be None.
    """
    spec_file = store.spec_path(cfg.home, key)
    overrides: dict = {}
    if spec_file.exists():
        overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))

    kind = overrides.get("kind", "implementation")
    if kind == "research":
        default_model = cfg.research_model
        default_effort: str | None = cfg.research_effort
    else:
        default_model = cfg.reconcile_model
        default_effort = cfg.default_effort

    model = overrides.get("model", default_model)
    effort = overrides.get("effort") or default_effort
    return model, effort


def _worker_cwd(cfg: Config, key: str) -> Path:
    """Where the reconciler runs. Prefer the ticket's own worktree; before one
    exists (e.g. the first triage step) fall back to the repo so the
    ``/maestro-reconcile`` command + skill resolve. Only if no repo is configured
    do we land in ``home`` (which has no ``.claude/commands`` — the reconciler
    would no-op there)."""
    wt = cfg.home / "worktrees" / key
    if wt.exists():
        return wt
    if cfg.repo_path:
        return Path(cfg.repo_path)
    return cfg.home


def _write_heartbeat(home: Path, now: float, spawned: int, active: int,
                     throttled: int, due: int) -> None:
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": store.iso_now(), "epoch": now,
                      "spawned": spawned, "active": active,
                      "throttled": throttled, "due": due})
