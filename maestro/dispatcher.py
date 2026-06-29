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
    """Drain the keyless ``_new`` inbox into real ticket dirs + TicketCreated events."""
    home = cfg.home
    minted: list[str] = []
    for _idx, entry in inbox.pending_new(home):
        prefix = entry.get("prefix") or None
        key = entry.get("key") or _auto_key(home, prefix=prefix or "T")
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


def _auto_key(home: Path, prefix: str = "T") -> str:
    n = 1
    while (home / "tickets" / f"{prefix}-{n}").exists():
        n += 1
    return f"{prefix}-{n}"


def _seed_spec(key: str, title: str, args: dict) -> str:
    tier = args.get("approval_tier", 1)
    depends_on = args.get("depends_on", [])
    deps_str = ", ".join(depends_on) if depends_on else ""
    lines = [
        f"# {key}: {title}",
        "",
        "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->",
        "",
        f"approval_tier: {tier}",
        f"priority: {args.get('priority', 3)}",
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
    lines.append(args.get("intent", "(describe what done looks like)"))
    if args.get("notes"):
        lines.append("")
        lines.append("## Notes")
        lines.append(args["notes"])
    lines.append("")
    lines.append("## Acceptance criteria")
    lines.append("- ")
    return "\n".join(lines) + "\n"


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

    slots = max(0, cfg.max_concurrency - len(active))
    to_spawn = due[:slots]
    capacity_skipped = [k for k, _ in due[slots:]]

    spawned: list[str] = []
    for key, _reason in to_spawn:
        cwd = _worker_cwd(cfg, key)
        prompt = f"{cfg.reconcile_command} {key}"
        model, effort = _resolve_model_effort(cfg, key)
        sessions.spawn(key, prompt, cwd, model=model, effort=effort)
        spawned.append(key)

    _write_heartbeat(home, now, len(spawned), len(active))
    return DispatchReport(
        minted=minted, due=due, claimed=claimed, spawned=spawned,
        capacity_skipped=capacity_skipped, active_sessions=len(active),
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


def _write_heartbeat(home: Path, now: float, spawned: int, active: int) -> None:
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": store.iso_now(), "epoch": now,
                      "spawned": spawned, "active": active})
