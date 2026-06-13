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

from dataclasses import dataclass
from pathlib import Path

from . import events as E
from . import event_log, inbox, snapshot as snap_mod, store
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


def is_due(snap: snap_mod.Snapshot, *, inbox_pending: bool,
           current_spec_hash: str | None, now: float) -> DueResult:
    phase = Phase(snap.phase)
    if phase in TERMINAL_PHASES:
        return DueResult(False, "terminal")
    if inbox_pending:
        return DueResult(True, "inbox")
    if current_spec_hash is not None and current_spec_hash != snap.spec_hash:
        return DueResult(True, "spec-changed")
    if phase in SLEEPING_PHASES:
        if snap.next_requeue_at is not None and snap.next_requeue_at <= now:
            return DueResult(True, "timer")
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
    return sorted(keys)


def mint_new_tickets(cfg: Config) -> list[str]:
    """Drain the keyless ``_new`` inbox into real ticket dirs + TicketCreated events."""
    home = cfg.home
    minted: list[str] = []
    for _idx, entry in inbox.pending_new(home):
        key = entry.get("key") or _auto_key(home)
        try:
            store.validate_key(key)
        except store.MaestroError:
            continue
        spec = store.spec_path(home, key)
        if not spec.exists():
            store.atomic_write(spec, _seed_spec(key, entry.get("title", key), entry.get("args", {})))
        event_log.append(
            home, key, E.TICKET_CREATED,
            {"title": entry.get("title", key), "source": "inbox/_new",
             "spec_hash": spec_hash_on_disk(home, key)},
            actor="dispatcher",
            step_id=f"create-{key}",
        )
        snap_mod.rebuild(home, key)
        minted.append(key)
    inbox.ack_new(home)
    return minted


def _auto_key(home: Path) -> str:
    n = 1
    while (home / "tickets" / f"T-{n}").exists():
        n += 1
    return f"T-{n}"


def _seed_spec(key: str, title: str, args: dict) -> str:
    tier = args.get("approval_tier", 1)
    return (
        f"# {key}: {title}\n\n"
        f"<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->\n\n"
        f"approval_tier: {tier}\n"
        f"priority: {args.get('priority', 3)}\n"
        f"dependsOn: []\n\n"
        f"## Intent\n{args.get('intent', '(describe what done looks like)')}\n\n"
        f"## Acceptance criteria\n- \n"
    )


@dataclass
class DispatchReport:
    minted: list[str]
    due: list[tuple[str, str]]      # (key, reason)
    claimed: list[str]              # due but a live session already owns it
    spawned: list[str]
    capacity_skipped: list[str]     # due + free but over the concurrency cap
    active_sessions: int


def dispatch(cfg: Config, sessions: SessionManager, now: float) -> DispatchReport:
    """One sweep. ``sessions`` decides whether spawns actually launch (use
    ``DryRunSessions`` to record-without-launch). Always idempotent and safe to
    run on a timer — minting and folding are no-ops when nothing changed.
    """
    home = cfg.home
    minted = mint_new_tickets(cfg)

    active = sessions.list_active()
    due: list[tuple[str, str]] = []
    claimed: list[str] = []

    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        # Keep the dispatcher's view fresh if a writer fell behind on its fold.
        if event_log.last_seq(home, key) > snap.observed_seq:
            snap = snap_mod.rebuild(home, key)
        res = is_due(
            snap,
            inbox_pending=inbox.has_pending(home, key),
            current_spec_hash=spec_hash_on_disk(home, key),
            now=now,
        )
        if not res.due:
            continue
        if key in active:
            claimed.append(key)        # per-key serialization: one reconciler per key
            continue
        due.append((key, res.reason))

    slots = max(0, cfg.max_concurrency - len(active))
    to_spawn = due[:slots]
    capacity_skipped = [k for k, _ in due[slots:]]

    spawned: list[str] = []
    for key, _reason in to_spawn:
        cwd = _worker_cwd(cfg, key)
        prompt = f"{cfg.reconcile_command} {key}"
        sessions.spawn(key, prompt, cwd)
        spawned.append(key)

    _write_heartbeat(home, now, len(spawned), len(active))
    return DispatchReport(
        minted=minted, due=due, claimed=claimed, spawned=spawned,
        capacity_skipped=capacity_skipped, active_sessions=len(active),
    )


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


def _write_heartbeat(home: Path, now: float, spawned: int, active: int) -> None:
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": store.iso_now(), "epoch": now,
                      "spawned": spawned, "active": active})
