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

import json
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path

from . import claims, events as E
from . import event_log, fleet, inbox, notify, ratelimit, schedule, snapshot as snap_mod, store
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
        if field in ("kind", "model", "effort", "repo"):
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
        if ticket_args.get("repo"):
            ticket_payload["repo"] = ticket_args["repo"]
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
    if args.get("repo"):
        lines.append(f"repo: {args['repo']}")
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


_REPO_PREFLIGHT_SENTINELS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_REPO_PREFLIGHT_DIRS = ("rebase-merge", "rebase-apply")


def repo_preflight(cfg: Config) -> dict:
    """Read-only guard: is ``cfg.repo_path`` safe to fast-forward and spawn a
    reconciler into right now?

    Three probes, all read-only, all against the SAME repo the dispatcher fast-
    forwards in ``sync_worktrees`` and spawns bare-repo reconcilers into (see
    ``_worker_cwd``): (a) an in-progress merge/rebase/cherry-pick/revert, via
    the sentinel files git leaves under the git dir; (b) unmerged index
    entries; (c) a tracked file carrying a REAL conflict hunk (both markers —
    a file that only quotes one, e.g. this feature's own tests/docs, must not
    block; see T-19 spec Notes on why ``--all-match`` is load-bearing).

    Fails OPEN — returns ``ok=True`` — on anything that makes the probe itself
    unusable (git missing, ``repo_path`` unset/absent, unborn HEAD, a hung
    subprocess): a flaky check that can permanently brick the fleet is a worse
    failure mode than the one this guards against, so only positive evidence
    of a real conflict blocks. Never raises into ``dispatch()``.
    """
    if not cfg.repo_preflight:
        return {"ok": True, "blockers": []}
    repo = cfg.repo_path
    if not repo or not Path(repo).exists():
        return {"ok": True, "blockers": ["preflight-unavailable"]}

    import subprocess

    def _run(args, timeout=10):
        try:
            return subprocess.run(["git", "-C", repo, *args],
                                  capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        # One rev-parse resolves the git dir AND validates HEAD is born: a repo
        # with an unborn HEAD (or no repo at all) makes this exit non-zero even
        # though the git-dir line alone would otherwise print fine.
        head_res = _run(["rev-parse", "--absolute-git-dir", "HEAD"])
        if head_res is None or head_res.returncode != 0 or not head_res.stdout.strip():
            return {"ok": True, "blockers": ["preflight-unavailable"]}
        gitdir = Path(head_res.stdout.splitlines()[0].strip())

        blockers: list[str] = []
        for name in _REPO_PREFLIGHT_SENTINELS:
            if (gitdir / name).exists():
                blockers.append(name)
        for name in _REPO_PREFLIGHT_DIRS:
            if (gitdir / name).exists():
                blockers.append(f"{name}/")

        unmerged = _run(["ls-files", "-u"])
        if unmerged is not None and unmerged.returncode == 0 and unmerged.stdout.strip():
            blockers.append("unmerged index entries (git ls-files -u)")

        # --all-match is load-bearing: a lone quoted marker (docs, this
        # feature's own tests) must not match; a real conflict hunk always
        # carries both.
        grep_res = _run(["grep", "-I", "-l", "--all-match",
                         "-e", "^<<<<<<< ", "-e", "^>>>>>>> ", "--", "."])
        if grep_res is not None and grep_res.returncode == 0 and grep_res.stdout.strip():
            paths = grep_res.stdout.strip().splitlines()
            shown = ", ".join(paths[:5])
            if len(paths) > 5:
                shown += f" (+{len(paths) - 5} more)"
            blockers.append(f"conflict markers in: {shown}")

        return {"ok": not blockers, "blockers": blockers}
    except Exception:
        return {"ok": True, "blockers": ["preflight-unavailable"]}


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


def _compact_cursor_path(home: Path) -> Path:
    return home / "derived" / ".compact_cursor.json"


def run_compact_tick(cfg: Config, now: float) -> dict:
    """Cursor-gated dispatcher tick: fold pre-snapshot events into the archive
    for every ticket whose folded log has grown past ``compact_min_events``, at
    most once per ``compact_interval`` seconds (0 disables — a manual
    ``maestro compact <key>`` always works regardless)."""
    interval = cfg.compact_interval
    if not interval or interval <= 0:
        return {"compacted": []}
    home = cfg.home
    cursor_path = _compact_cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    if now - cursor.get("epoch", 0) < interval:
        return {"compacted": []}

    from . import ops

    compacted = []
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        if snap.observed_seq < cfg.compact_min_events:
            continue
        result = ops.compact(cfg, key)
        if result.get("archived"):
            compacted.append(key)

    cursor["epoch"] = now
    store.write_json(cursor_path, cursor)
    return {"compacted": compacted}


def run_archive_tick(cfg: Config, now: float) -> dict:
    """Dispatcher tick: archive DONE tickets past their ``archive_after`` grace
    period (``None`` disables the tick entirely). Cheap and idempotent to run
    every sweep -- an already-archived key is absent from ``list_keys`` and a
    not-yet-old-enough DONE ticket is a fast no-op skip."""
    if cfg.archive_after is None:
        return {"archived": []}

    from . import ops

    archived = ops.archive_done(cfg, after=cfg.archive_after, now=now)
    return {"archived": archived}


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

    from . import providers, repos  # lazy: avoid a hard import-time dependency

    vcs = providers.get_vcs(cfg)
    checked = 0
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        phase = Phase(snap.phase)
        if phase not in (Phase.AWAITING_CI, Phase.IN_REVIEW) or not snap.pr_number:
            continue
        checked += 1
        repo_slug = repos.resolve_vcs_slug(cfg, snap)
        status = vcs.pr_status(snap.pr_number, repo=repo_slug)

        if _route_if_merged(cfg, key, status):
            continue
        if status.get("mergeable") == "CONFLICTING":
            from . import ops
            ops.route_conflict(cfg, key, snap.pr_number, actor="dispatcher")
            continue

        _observe_ci(cfg, key, status, phase)
        _observe_reviews(cfg, key, snap.pr_number, vcs, repo=repo_slug)

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


def _observe_reviews(cfg: Config, key: str, pr_number: int, vcs, repo: str | None = None) -> None:
    changes_requested_body: str | None = None
    for r in vcs.review_feedback(pr_number, repo=repo):
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


def maintenance_tick(home: Path, name: str, interval: int, now: float, fn) -> dict | None:
    """Generic cursor-gated maintenance tick: run ``fn()`` at most once per
    ``interval`` seconds, persisted in ``derived/.<name>_cursor.json``
    (level-triggered, idempotent — the same shape ``backup.maybe_backup`` already
    uses). ``interval <= 0`` disables the tick entirely. Returns ``fn()``'s result,
    or ``None`` when the tick didn't run this sweep. Left generic so other
    maintenance ticks (e.g. L-12's compact/archive_done) can adopt it instead of
    hand-rolling another cursor.
    """
    if not interval or interval <= 0:
        return None
    cursor_path = home / "derived" / f".{name}_cursor.json"
    cursor = store.read_json(cursor_path, {}) or {}
    if now - cursor.get("epoch", 0) < interval:
        return None
    result = fn()
    cursor["epoch"] = now
    store.write_json(cursor_path, cursor)
    return result


def prune_logs_tick(cfg: Config, now: float) -> dict | None:
    """Cursor-gated session-log pruning tick — see ``ops.prune_all_session_logs``."""
    from . import ops
    return maintenance_tick(cfg.home, "prune", cfg.prune_interval, now,
                            lambda: ops.prune_all_session_logs(cfg, now=now))


def run_watchdog(cfg: Config, now: float) -> list[str]:
    """Reap any claim whose session has run past ``max_session_seconds`` (0
    disables). ``claims.active_keys`` only checks pid-alive, so a live-but-stuck
    claude session holds its key forever; this is the age-based backstop, and it
    must run before ``active`` is computed so a hung claim never counts toward
    concurrency. Best-effort SIGTERM to the process group (pid == pgid, since
    sessions spawn with ``start_new_session=True``) -- a pid that's already gone
    is not an error, just one less thing to kill."""
    home = cfg.home
    max_seconds = cfg.max_session_seconds
    if not max_seconds:
        return []
    reaped: list[str] = []
    for key, claim in claims.all_claims(home).items():
        epoch = claim.get("epoch")
        if not isinstance(epoch, (int, float)) or now - epoch <= max_seconds:
            continue
        pid = claim.get("pid")
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError, TypeError, OSError):
            pass
        claims.release(home, key)
        from . import ops
        ops.fail(cfg, key, f"watchdog: session exceeded {max_seconds}s (pid {pid})",
                actor="dispatcher")
        reaped.append(key)
    return reaped


def _spawn_attempts_path(home: Path) -> Path:
    return home / "derived" / ".spawn_attempts.json"


def _allow_spawn(cfg: Config, key: str, observed_seq: int, attempts: dict) -> bool:
    """Gate one spawn of *key* against the no-progress counter, mutating
    ``attempts`` in place. Returns False (and fails the ticket instead) once the
    key has been spawned ``max_spawn_attempts`` times at the same observed_seq --
    i.e. it crashed before appending a single event, every time. Any real
    progress (observed_seq advancing) resets the count to zero."""
    max_attempts = cfg.max_spawn_attempts
    if not max_attempts:
        return True
    entry = attempts.get(key)
    if not entry or entry.get("seq") != observed_seq:
        entry = {"seq": observed_seq, "count": 0}
    if entry["count"] >= max_attempts:
        from . import ops
        ops.fail(cfg, key,
                 f"watchdog: {entry['count']} spawns with no progress at seq {observed_seq}",
                 actor="dispatcher")
        attempts.pop(key, None)
        return False
    entry["count"] += 1
    attempts[key] = entry
    return True


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
    pruned_logs: int = 0
    pruned_bytes: int = 0
    errors: dict = field(default_factory=dict)  # tick name -> error string (never aborts the sweep)
    paused_until: float | None = None  # fleet-wide rate-limit gate deadline, if paused
    reaped: list[str] = field(default_factory=list)     # watchdog: killed for age or no-progress
    paused: bool = False            # the fleet.pause() kill switch was armed this sweep
    repo_blockers: list[str] = field(default_factory=list)  # non-empty -> spawn step was skipped
    hook_errors: dict = field(default_factory=dict)     # hook name -> "ExcType: message"


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


def _run_hook(name: str, hook_errors: dict, fn, *args, default=None, **kwargs):
    """Run one dispatch hook, recording (not raising) any exception.

    A single tracker/network/backup hook blowing up used to halt the ENTIRE
    sweep -- no due-computation even ran, so nothing spawned, with only a
    stale heartbeat as the symptom. Every hook now runs in isolation: a
    failure is recorded under its name in ``hook_errors`` (surfaced in
    ``maestro why``/``derived/dispatch.jsonl``) and the sweep proceeds with
    ``default`` in place of that hook's result.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - a hook must never take the sweep down with it
        hook_errors[name] = f"{type(e).__name__}: {e}"
        return default


# --- per-sweep decision ledger (`derived/dispatch.jsonl` + `maestro why`) ----

def dispatch_ledger_path(home: Path) -> Path:
    return home / "derived" / "dispatch.jsonl"


# Cap on how many sweep records the ledger retains -- one line per sweep, so an
# unbounded launchd cadence (the 2026-07-19 regime: sweeps every ~11s) must not
# grow the file without bound. 500 sweeps is generous history for `maestro why`.
_DISPATCH_LEDGER_MAX_LINES = 500


def _append_dispatch_ledger(home: Path, record: dict) -> None:
    path = dispatch_ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with store.file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines.append(json.dumps(record, separators=(",", ":"), default=str))
        lines = lines[-_DISPATCH_LEDGER_MAX_LINES:]
        tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)


def key_decisions(home: Path, key: str, *, tail: int = 20) -> list[dict]:
    """The most recent ``tail`` sweep decisions recorded for *key* (oldest
    first), each ``{"ts", "outcome", "reason", "hook_errors"}`` -- what
    ``maestro why`` prints."""
    records = store.read_jsonl(dispatch_ledger_path(home))
    out = []
    for rec in records:
        decision = (rec.get("decisions") or {}).get(key)
        if decision is None:
            continue
        out.append({
            "ts": rec.get("ts"),
            "outcome": decision.get("outcome"),
            "reason": decision.get("reason"),
            "hook_errors": rec.get("hook_errors") or None,
        })
    return out[-tail:] if tail else out


def dispatch(cfg: Config, sessions: SessionManager, now: float) -> DispatchReport:
    """One sweep. ``sessions`` decides whether spawns actually launch (use
    ``DryRunSessions`` to record-without-launch). Always idempotent and safe to
    run on a timer — minting and folding are no-ops when nothing changed.
    """
    home = cfg.home

    if fleet.pause_state(home, now) is not None:
        # The kill switch. Ahead of EVERYTHING else — mint/sync/scheduled-tasks/
        # worktrees/backup/sessions all stay untouched, and no due-computation
        # even runs, so there is no due-reason (not even an _UNTHROTTLED_REASONS
        # human signal) that could slip past it. The one permitted side effect
        # is the heartbeat, so a paused board never reads as a dead dispatcher.
        _write_heartbeat(home, now, 0, 0, paused=True)
        return DispatchReport(
            minted=[], due=[], claimed=[], spawned=[], capacity_skipped=[],
            active_sessions=0, paused=True,
        )

    from . import backup  # lazy: backup -> projection -> dispatcher would cycle at import time
    from . import health  # lazy: health -> dispatcher would cycle at import time

    hook_errors: dict = {}
    minted = _run_hook("mint_new_tickets", hook_errors, mint_new_tickets, cfg, default=[])
    _run_hook("sync_external_sources", hook_errors, sync_external_sources, cfg, now)
    scheduled_fired = _run_hook("run_scheduled_tasks", hook_errors, run_scheduled_tasks,
                                cfg, now, default={"fired": []})["fired"]
    preflight = repo_preflight(cfg)
    repo_ok = preflight["ok"]
    repo_blockers = list(preflight["blockers"]) if not repo_ok else []
    if repo_ok:
        _run_hook("sync_worktrees", hook_errors, sync_worktrees, cfg)
    _run_hook("sync_vcs", hook_errors, sync_vcs, cfg, now)
    _run_hook("backup", hook_errors, backup.maybe_backup, cfg, now)
    _run_hook("compact_tick", hook_errors, run_compact_tick, cfg, now)
    _run_hook("archive_tick", hook_errors, run_archive_tick, cfg, now)
    _run_hook("ratelimit_probe", hook_errors, ratelimit.probe, cfg, now)
    _run_hook("notify", hook_errors, notify.maybe_notify, cfg, now)

    # Watchdog runs before `active` is computed: a hung claim must not count
    # toward concurrency, and a reaped key needs to be re-fold-visible (fail
    # appends an event) before this sweep's due-check reads its snapshot.
    reaped = _run_hook("watchdog", hook_errors, run_watchdog, cfg, now, default=[])

    pruned_logs = 0
    pruned_bytes = 0
    errors: dict = {}
    prune_result = _run_hook("prune_tick", hook_errors, prune_logs_tick, cfg, now, default=None)
    if prune_result:
        pruned_logs = prune_result.get("pruned_logs", 0)
        pruned_bytes = prune_result.get("pruned_bytes", 0)
        per_key_errors = prune_result.get("errors") or {}
        if per_key_errors:
            errors["prune"] = "; ".join(f"{k}: {v}" for k, v in per_key_errors.items())

    active = sessions.list_active()
    due: list[tuple[str, str]] = []
    claimed: list[str] = []
    observed_seq_by_key: dict[str, int] = {}
    decisions: dict[str, dict] = {}

    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        # Keep the dispatcher's view fresh if a writer fell behind on its fold.
        if event_log.last_seq(home, key) > snap.observed_seq:
            snap = snap_mod.rebuild(home, key)
        observed_seq_by_key[key] = snap.observed_seq
        blocked_dep = _has_unmet_deps(home, key)
        res = is_due(
            snap,
            inbox_pending=inbox.has_pending(home, key),
            current_spec_hash=spec_hash_on_disk(home, key),
            now=now,
            blocked_dep=blocked_dep,
        )
        if not res.due:
            decisions[key] = {"outcome": "not_due", "reason": res.reason}
            continue
        if key in active:
            claimed.append(key)        # per-key serialization: one reconciler per key
            decisions[key] = {"outcome": "claimed", "reason": res.reason}
            continue
        due.append((key, res.reason))
        decisions[key] = {"outcome": "due", "reason": res.reason}

    # Fleet-wide rate-limit gate. Above the human-signal bypass below: an inbox
    # answer must not punch through a 429, since that spawn would be rejected too.
    # While paused, nothing spawns and the spawn ledger is left untouched.
    # A conflict-marked/mid-merge repo (repo_ok is False) gates the spawn step the
    # same way — it is NOT bypassable by an _UNTHROTTLED_REASONS human signal,
    # because a human answering a question is exactly the moment you must not
    # launch an agent into a half-merged tree. sync_worktrees() (the ff-only
    # merge) was already skipped above for the same reason.
    paused_until_ts = ratelimit.paused_until(home, now)
    if paused_until_ts is not None or not repo_ok:
        spawned: list[str] = []
        throttled: list[str] = []
        capacity_skipped: list[str] = []
    else:
        # Spawn-rate floor. The claim file is the ONLY other per-key spawn memory and it
        # is unlinked the moment the worker dies — so a session that exits in under a
        # second (a rate-limit rejection, a crash) leaves nothing behind and the key is
        # instantly re-spawnable. This ledger outlives the process and bounds the rate no
        # matter how often the dispatcher itself is fired.
        floor = spawn_floor(cfg)
        ledger_path = _spawn_ledger_path(home)
        ledger = store.read_json(ledger_path, {}) or {}
        throttled = []
        if floor:
            eligible: list[tuple[str, str]] = []
            for key, reason in due:
                entry = ledger.get(key)
                last = entry.get("last") if isinstance(entry, dict) else entry
                if (reason not in _UNTHROTTLED_REASONS
                        and isinstance(last, (int, float)) and now - last < floor):
                    throttled.append(key)
                    decisions[key]["outcome"] = "throttled"
                    continue
                eligible.append((key, reason))
        else:
            eligible = due

        slots = max(0, cfg.max_concurrency - len(active))
        to_spawn = eligible[:slots]
        capacity_skipped = [k for k, _ in eligible[slots:]]
        for key in capacity_skipped:
            decisions[key]["outcome"] = "capacity_skipped"

        attempts_path = _spawn_attempts_path(home)
        attempts = store.read_json(attempts_path, {}) or {}
        attempts_changed = False

        spawned = []
        for key, _reason in to_spawn:
            if not _allow_spawn(cfg, key, observed_seq_by_key.get(key, 0), attempts):
                attempts_changed = True
                reaped.append(key)
                decisions[key]["outcome"] = "attempts_exhausted"
                continue
            attempts_changed = True
            cwd = _worker_cwd(cfg, key)
            prompt = f"{cfg.reconcile_command} {key}"
            model, effort = _resolve_model_effort(cfg, key)
            sessions.spawn(key, prompt, cwd, model=model, effort=effort)
            spawned.append(key)
            decisions[key]["outcome"] = "spawned"
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
        if attempts_changed:
            known = set(list_keys(home))
            store.write_json(attempts_path,
                             {k: v for k, v in attempts.items() if k in known})

    _write_heartbeat(home, now, len(spawned), len(active), len(throttled), len(due),
                     repo_blockers=repo_blockers)
    _append_dispatch_ledger(home, {
        "ts": store.iso_now(), "epoch": now,
        "hook_errors": hook_errors, "decisions": decisions,
    })
    return DispatchReport(
        minted=minted, due=due, claimed=claimed, spawned=spawned,
        capacity_skipped=capacity_skipped, active_sessions=len(active),
        scheduled_fired=scheduled_fired, throttled=throttled,
        pruned_logs=pruned_logs, pruned_bytes=pruned_bytes, errors=errors,
        paused_until=paused_until_ts, reaped=reaped,
        repo_blockers=repo_blockers, hook_errors=hook_errors,
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
                     throttled: int = 0, due: int = 0, *, paused: bool = False,
                     repo_blockers: list[str] | None = None) -> None:
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": store.iso_now(), "epoch": now,
                      "spawned": spawned, "active": active,
                      "throttled": throttled, "due": due, "paused": paused,
                      "repo_blockers": repo_blockers or []})
