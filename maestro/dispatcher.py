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
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import alarm, claims, credentials, events as E
from . import event_log, fleet, inbox, notify, ratelimit, schedule, snapshot as snap_mod, spend, steplog, store
from .config import Config
from .gates import backend_interlock_reason, parse_spec_overrides, spec_priority, spec_runner  # noqa: F401 (re-export)
from .idempotency import content_hash
from .sessions import SessionManager
from .statemachine import ACTIVE_PHASES, Phase, SLEEPING_PHASES, TERMINAL_PHASES


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


def split_key(key: str) -> tuple:
    """Natural sort key: (0, prefix, number) for well-formed keys, (1, key, 0) for others."""
    m = _KEY_RE.match(key)
    if m:
        return (0, m.group(1), int(m.group(2)))
    return (1, key, 0)


def _rotation_cursor_path(home: Path) -> Path:
    return home / "derived" / ".rotation_cursor.json"


def _apply_rotation(home: Path, due: list, bindings_by_key: dict, cursor: dict) -> list:
    """T-63 (MTO-9 defect 4b): rotate the same-priority tiebreak, scoped per
    repo, so repeated sweeps under sustained pressure don't always favor the
    lexically-first `split_key` among equal-priority competitors. `due` must
    already be sorted (priority, split_key) -- that stays the base/fallback
    order verbatim; this only reshuffles the STARTING POINT within each
    (repo, priority) group of size > 1, never the group membership or the
    across-priority order, so MTO-7's own guarantees (priority never bypasses
    a brake, total deterministic order) are unaffected. `cursor` is
    `{repo_name: {str(priority): last_spawned_key}}`, read once by the caller
    via `store.read_json` (already degrades a missing/corrupt file to `{}`
    without raising) -- a `last_spawned_key` absent from its group (stale,
    or never set) leaves that group in its base order, so a cold cursor is
    provably identical to today's behavior.
    """
    groups: dict[tuple[str, int], list] = {}
    order: list[tuple[str, int]] = []
    for key, reason in due:
        binding = bindings_by_key.get(key)
        repo_name = binding.name if binding is not None else "default"
        gkey = (repo_name, spec_priority(home, key))
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append((key, reason))

    rotated: list = []
    for gkey in order:
        group = groups[gkey]
        if len(group) > 1:
            repo_name, priority = gkey
            last_key = (cursor.get(repo_name) or {}).get(str(priority))
            idx = next((i for i, (k, _) in enumerate(group) if k == last_key), None)
            if idx is not None:
                group = group[idx + 1:] + group[:idx + 1]
        rotated.extend(group)
    return rotated


def parse_depends_on(spec_text: str) -> list[str]:
    """Extract the dependsOn list from a spec's loose frontmatter."""
    m = _DEPENDS_ON_RE.search(spec_text)
    if not m:
        return []
    raw = m.group(1)
    return [k.strip() for k in raw.split(",") if k.strip()]


# AD-7: merging is always a human decision, unconditionally -- every spawned
# reconciler is denied `gh pr merge` regardless of ticket or phase. Formerly
# gated by approval tier (`tier_denylist`, tier>=1 only); tier is gone, so this
# is now a flat constant, unioned with `phase_denylist` at the same spawn site
# as before (see the `sessions.spawn` call in `dispatch()` below).
MERGE_DENYLIST = ["Bash(gh pr merge:*)"]


def phase_denylist(phase: str) -> list[str]:
    """Tool-surface denylist for a spawned reconciler, keyed by phase (RF-6).

    Unioned with ``MERGE_DENYLIST`` at spawn time. Empty for every phase except
    ``qa``: a QA reconciler judges a diff independently of the agent that wrote
    it, so it must not be able to Edit or Write the code it's reviewing.
    """
    try:
        return ["Edit", "Write"] if Phase(phase) == Phase.QA else []
    except ValueError:
        return []


def resolved_allowed_tools(cfg: Config, binding) -> list[str]:
    """Per-key --allowedTools additions (GA-10): the board-wide
    ``reconcile_allowed_tools`` list unioned with *binding*'s own
    ``reconcile_allowed_tools`` -- an unset/empty repo list simply contributes
    nothing extra, so it inherits the board-wide list rather than replacing it.
    Threaded through ``sessions.spawn`` per key, beside ``MERGE_DENYLIST``, and
    merged by the session backend with the process-wide base grant (maestro CLI
    verbs + ``reconcile_web_tools``) into exactly ONE ``--allowedTools`` flag
    (see ``sessions.ClaudeCliSessions.spawn``).
    """
    merged = list(cfg.reconcile_allowed_tools)
    if binding is not None:
        for tool in binding.reconcile_allowed_tools:
            if tool not in merged:
                merged.append(tool)
    return merged


def resolve_credential(binding, cache: dict) -> credentials.CredentialResolution:
    """*binding*'s resolved gh credential overlay (GA-17), memoized in *cache*
    (caller-owned, keyed by ``(gh_account, token_env)``) so N keys bound to the
    same repo resolve the token once per sweep -- see ``maestro.credentials``.
    ``binding is None`` (defensive -- mirrors ``resolved_allowed_tools``'s own
    None-handling; shouldn't happen for a key already in ``bindings_by_key``)
    resolves to "nothing configured", same as a binding with neither field set.
    """
    gh_account = binding.gh_account if binding is not None else None
    token_env = binding.token_env if binding is not None else None
    return credentials.resolve_cached(gh_account, token_env, cache)


# GA-16: the verb whitelist a spawned reconciler is granted via one
# ``Bash(maestro <verb>:*)`` rule per entry, unioned into --allowedTools at
# spawn time (``cli._reconciler_tool_grants``). Lives here, not cli.py, so
# ``health.check_reconciler_permissions`` can read the very same tuple
# ``cli._reconciler_tool_grants`` builds from without an import cycle
# (cli.py already imports this module; the reverse would be circular) --
# MTO-5, replacing a hand-maintained duplicate in ``RECONCILER_REQUIRED_TOOLS``
# below that had silently drifted from what GA-3 actually emits.
# ``cli._AGENT_TOOL_VERBS`` re-exports this tuple under its old name for
# backward-compat imports.
#
# NEVER add "approve" (self-clears the tier-2 gate AD-1 exists to enforce),
# "restore" (the one irreversible verb), "fleet" (launchd + the pause kill
# switch), or any other human-only verb here -- and never collapse this to
# the bare wildcard "maestro:*", which grants all of those at once.
AGENT_TOOL_VERBS = (
    # The 23 "[agent]"-tagged verbs registered in build_parser().
    "local-backup", "snapshot", "events", "append", "set-phase", "ask",
    "fold-inbox", "inbox-ack", "observe-spec", "requeue", "fail", "impl-turn",
    "verify-ac", "qa-brief", "qa-verdict", "capture-tests", "finalize", "checked", "release",
    "check-conflicts", "check-merged", "fold-steps", "worktree",
    # Not "[agent]"-tagged, but genuinely invoked by skills (grep skills/*.md):
    "env",     # every phase preamble's first command, all phase files
    "show",    # maestro-reconcile-passive.md reads pending_inbox through it
    "create",  # maestro-reconcile-awaiting-human.md mints implementation tickets
)

# The coarse wildcard a human grants by hand in a settings file (see this
# repo's own ``.claude/settings.json``) -- a different trust boundary than the
# spawner emitting it itself (AD-1's concern is specifically the spawner
# self-widening), so it stays an accepted -- but no longer the *only*
# accepted -- form of the 'maestro' requirement below. MUST equal
# ``AGENT_TOOL_VERBS``'s render prefix; ``cli._reconciler_tool_grants``
# asserts this constant is ``RECONCILER_REQUIRED_TOOLS[0]`` so the two can
# never silently drift apart.
MAESTRO_COARSE_GRANT = "Bash(maestro:*)"


def maestro_verb_grant(verbs=AGENT_TOOL_VERBS) -> list[str]:
    """The narrowed, verb-scoped ``Bash(maestro <verb>:*)`` rules a spawned
    reconciler is actually granted -- the literal form GA-3 emits in place of
    the bare ``Bash(maestro:*)`` wildcard, which the spawner is tested to
    never emit (AD-1, ``tests/test_web_tools.py::test_bare_wildcard_never_appears``).
    MTO-5: the single source both ``cli._reconciler_tool_grants`` (spawn time)
    and ``health.check_reconciler_permissions`` (the permission check) build
    from, so the emitted grant and the required grant can't drift the way the
    old hand-maintained ``RECONCILER_REQUIRED_TOOLS`` tuple did.
    """
    return [f"Bash(maestro {verb}:*)" for verb in verbs]


# GA-16 / MTO-5: the spawned-reconciler Bash surface a bound repo's Claude Code
# settings must grant, as ``health.check_reconciler_permissions`` checks it.
# Two already-specified sources, unioned:
#   (1) GA-3's maestro-verb grant, represented here by ``MAESTRO_COARSE_GRANT``
#       -- but ``check_reconciler_permissions`` treats this one entry
#       specially (dual acceptance, decided explicitly in MTO-5 rather than
#       drifted into): a repo's settings satisfy it via EITHER this coarse
#       wildcard OR the full narrowed ``maestro_verb_grant()`` set (the
#       literal form the spawner actually emits) -- so the requirement is
#       satisfiable by what GA-3 really emits, not only by a pattern the
#       spawner is forbidden to emit. The remaining entries below are plain
#       literals, matched exact-string (see ``check_reconciler_permissions``'s
#       detail message).
#   (2) the git/gh/test-runner commands
#       ``skills/maestro-reconcile-implementing.md`` runs post-GA-12 --
#       fetch/rebase, add/commit/push, ``gh pr create``/``gh pr view``.
#       ``.venv/bin/`` -- maestro's own venv, used by that skill's
#       ``.venv/bin/python -m pytest`` -- used to live in this board-wide
#       tuple too, demanded of every bound repo regardless of toolchain
#       (wrong: a yarn/Bazel-bound repo has no such path and never will,
#       MTO-5). It now lives in ``MAESTRO_OWN_REPO_EXTRA_TOOLS`` below and is
#       required only of the binding that resolves to maestro's own repo
#       checkout (``health._is_maestro_own_repo``).
# GA-10's per-repo ``reconcile_allowed_tools`` is the *mechanism* a human uses
# to grant (2) via ``--allowedTools`` instead of a settings file -- not a fixed
# pattern list itself, so there is nothing to union in from it here.
# NOT the preamble: GA-10 already shrank every phase preamble to almost no
# bash, so a preamble-derived list would collapse to just the maestro verbs
# and miss exactly the git/gh/test-runner gap this ticket exists to catch.
RECONCILER_REQUIRED_TOOLS = (
    MAESTRO_COARSE_GRANT,
    "Bash(git:*)",
    "Bash(gh:*)",
    "Bash(python3:*)",
)

# MTO-5: required only of the binding that resolves to maestro's own repo
# checkout -- see ``health._is_maestro_own_repo``. A foreign yarn/Bazel-bound
# repo is never measured against this.
MAESTRO_OWN_REPO_EXTRA_TOOLS = (
    "Bash(.venv/bin/:*)",
)


def is_due(home: Path, key: str, snap: snap_mod.Snapshot, *, inbox_pending: bool,
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
    # RB-9: this used to be an unconditional `return DueResult(True, "active")` --
    # anything that fell through terminal/sleeping/needs-approval above was
    # active by default, so an unclassified phase silently got spawned forever
    # (2026-07-19, 2026-08-14). PHASE_CLASS is asserted exhaustive at import
    # time, so this branch should only ever see an ACTIVE_PHASES member -- but
    # check explicitly and fail closed (never spawn) rather than fall through
    # to active if that invariant is ever wrong.
    if phase not in ACTIVE_PHASES:
        return DueResult(False, "unclassified")
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


def _never_minted(home: Path, key: str) -> bool:
    """True if *key* has no spec.md, no ticket directory, and no event-log
    history -- i.e. it was never minted via ``maestro create`` (every real
    minting path, ``_mint_ticket`` below included, writes spec.md before its
    first event -- see its docstring) and has never had a single event
    recorded against it either. RB-17: this is the shape a reconciler finds
    when it's spawned for a key that doesn't exist -- the guard here (and its
    twin, ``ops._refuse_unminted``) is what stops that discovery from minting
    the key by the mere act of failing against it.

    A key can be a ``list_keys`` member -- has SOME derived-only footprint,
    e.g. a stray ``derived/snapshots/<key>.json`` left over from a fold that
    ran before this guard existed -- while still being ``_never_minted``:
    that footprint carries no real history, only a fold artifact, so this
    checks the three ACTUAL sources of truth (ticket dir, spec.md, event log)
    rather than trusting ``list_keys`` membership.

    Deliberately False for a key with event-log history even if spec.md is
    now missing (deleted, or never restored) -- that key already has a real
    history; ``maestro cmd <KEY> discard`` must keep retiring it end to end
    (RB-17 AC4). This function never touches ``list_keys``'s own
    archived/dead-letter exclusion.
    """
    if store.spec_path(home, key).exists():
        return False
    if store.ticket_dir(home, key).exists():
        return False
    return not event_log.read(home, key)


def existing_prefixes(home: Path) -> list[str]:
    """Return sorted unique prefixes from all existing well-formed ticket keys."""
    seen: set[str] = set()
    for key in list_keys(home):
        m = _KEY_RE.match(key)
        if m:
            seen.add(m.group(1))
    return sorted(seen)


def _case_collision(home: Path, key: str) -> str | None:
    """Return an already-minted key that differs from *key* only by letter case,
    or ``None``. Filesystem-detected (probes the same case-insensitivity every
    volume either has or doesn't, never assumed) via ``list_keys``, so it costs
    an O(n) scan of existing keys -- deliberately kept OUT of ``validate_key``
    (a hot path hit on every path construction) and run only here, at
    ticket-creation time (RB-3, case (i): reject rather than canonicalise, since
    that's a creation-time check with no effect on already-minted keys)."""
    lower = key.lower()
    for existing in list_keys(home):
        if existing != key and existing.lower() == lower:
            return existing
    return None


def _report_rejected_mint(home: Path, key: str, reason: str) -> None:
    """A create request that couldn't be minted -- reported via the existing
    dead-letter surface (``tickets/_deadletter/*.md``, already surfaced by
    ``maestro doctor`` / ``health.check_dead_letters``) instead of the previous
    silent ``except MaestroError: continue`` drop (RB-3).

    The filename is derived from *key*, never *key* itself interpolated raw --
    an inbox-supplied key can fail ``validate_key`` for being path-unsafe in the
    first place (traversal, odd characters), and trusting it as a path component
    here would reopen exactly the hole ``validate_key`` exists to close. No
    ticket dir or event log exists for a rejected key, so this never collides
    with a real ticket's own dead-letter file (which is named ``<key>.md``).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:128] or "unknown"
    path = home / "tickets" / "_deadletter" / f"rejected-{safe}.md"
    body = (
        f"# Rejected create request: {key!r}\n\n"
        f"Reason: {reason}\n\n"
        "This key was never minted -- no ticket dir or event log exists for it.\n"
    )
    store.atomic_write(path, body)


def _mint_lock_target(home: Path) -> Path:
    """Fed to ``store.file_lock`` (which locks a sidecar ``.<name>.lock`` next to
    it, e.g. ``derived/.mint.lock``) to guard the "pick an auto-key, then create
    its ticket dir + TicketCreated event" critical section -- shared by the
    batch ``_new``-inbox drain (``mint_new_tickets``) and the synchronous
    single-ticket mint ``create --json`` performs (``mint_one``), so the two can
    never race each other onto the same auto-assigned key."""
    return home / "derived" / "mint"


def _spawn_lock_target(home: Path) -> Path:
    """Fed to ``store.file_lock`` (-> sidecar ``derived/.spawn.lock``) to guard
    dispatch()'s read-active / compute-slots / spawn region (T-52). Nothing
    previously serialised that region: launchd's timer tick and the in-process
    ``cli._nudge`` sweep (fired on every ``maestro ans``/``cmd``/``create``) could
    overlap, both read the same stale ``active`` set, both decide the same key
    has a free slot, and both spawn it -- and since the claim was written AFTER
    Popen (see ``sessions.ClaudeCliSessions.spawn``), the second write clobbered
    the first, leaving one live reconciler with NO claim: invisible to
    ``max_concurrency`` and to ``run_watchdog`` (which iterates claims only), so
    the orphan was never reaped. Holding this lock from the ``active`` read
    through the very last claim commit in the spawn loop makes that whole
    decision one atomic step: a second, concurrent ``dispatch()`` call blocks
    here and only proceeds once the first has fully committed its spawns, so it
    always sees them in the ``active`` set it reads next. Scoped to exactly this
    region (not the whole sweep) so the mint/sync/backup/watchdog hooks above it
    -- several of which touch the network -- never contend with each other's
    dispatch() calls over this lock."""
    return home / "derived" / "spawn"


def _mint_ticket(home: Path, key: str, title: str, ticket_args: dict, *,
                  actor: str, source: str) -> None:
    """Seed the spec (if absent) and append the ``TicketCreated`` event for an
    already-resolved, already-validated *key*. The tail both mint paths share."""
    spec = store.spec_path(home, key)
    title = title or key
    if not spec.exists():
        store.atomic_write(spec, _seed_spec(key, title, ticket_args))
    ticket_payload: dict = {
        "title": title,
        "source": source,
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
        actor=actor,
        step_id=f"create-{key}",
    )
    snap_mod.rebuild(home, key)


def mint_one(cfg: Config, *, key: str | None, prefix: str | None, title: str,
             ticket_args: dict, actor: str = "reconciler") -> str:
    """Synchronously mint exactly one ticket -- the ``create --json`` counterpart
    to the batch ``_new``-inbox drain ``mint_new_tickets`` performs on a sweep.
    Bypasses the inbox entirely, so the caller learns the assigned key
    immediately instead of having to wait for (and then re-derive) the next
    dispatcher sweep's mint.

    Locked (``_mint_lock_target``) against a concurrent call -- this or another
    process's sweep -- racing to pick the same auto-key. Raises
    ``MaestroError`` on an invalid or case-colliding explicit key. A key that
    already has events is treated as an idempotent no-op (retry-safe, exactly
    like ``mint_new_tickets``' own crash-safety semantics) and its existing key
    is returned unchanged rather than re-minted.
    """
    home = cfg.home
    with store.file_lock(_mint_lock_target(home)):
        resolved = key or _auto_key(home, prefix=prefix or "T")
        store.validate_key(resolved)
        collision = _case_collision(home, resolved)
        if collision:
            raise store.MaestroError(
                f"{resolved!r} differs from existing key {collision!r} only by "
                "letter case -- would alias its event log on a case-insensitive filesystem"
            )
        if event_log.last_seq(home, resolved) > 0:
            return resolved  # already minted -- idempotent no-op
        _mint_ticket(home, resolved, title, ticket_args, actor=actor, source="cli/create --json")
        return resolved


def mint_new_tickets(cfg: Config) -> list[str]:
    """Drain the keyless ``_new`` inbox into real ticket dirs + TicketCreated events.

    A create-request may carry a period-bucket ``dedup`` token (scheduled tasks do —
    see ``run_scheduled_tasks``) to close the window between that tick's ``_new``
    append and its cursor write: if a crash there causes the same period to be
    re-fired, the re-fired request carries the same token and is skipped here,
    making the re-fire a guaranteed no-op instead of a duplicate ticket.

    A request whose key would alias an existing key -- either textually
    (``validate_key``, e.g. a trailing ``.archive``) or on a case-insensitive
    filesystem (``_case_collision``) -- is rejected rather than minted, and the
    rejection is reported via ``_report_rejected_mint`` instead of dropped (RB-3).

    Locked (``_mint_lock_target``) around the per-entry auto-key-through-mint
    section so a concurrent synchronous ``mint_one`` call can never pick the
    same auto-assigned key as this sweep.
    """
    home = cfg.home
    minted: list[str] = []
    minted_path = home / "derived" / ".schedule_minted.json"
    minted_dedup = store.read_json(minted_path, {}) or {}
    dedup_changed = False
    lock_target = _mint_lock_target(home)
    for _idx, entry in inbox.pending_new(home):
        ticket_args = entry.get("args") or {}
        dedup = ticket_args.get("dedup")
        if dedup and dedup in minted_dedup:
            continue  # already minted this period — guaranteed no-op
        prefix = entry.get("prefix") or None
        with store.file_lock(lock_target):
            key = entry.get("key") or _auto_key(home, prefix=prefix or "T")
            try:
                store.validate_key(key)
            except store.MaestroError as e:
                _report_rejected_mint(home, key, str(e))
                continue
            collision = _case_collision(home, key)
            if collision:
                _report_rejected_mint(
                    home, key,
                    f"differs from existing key {collision!r} only by letter case -- "
                    "would alias its event log on a case-insensitive filesystem",
                )
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
            title = entry.get("title") or key
            _mint_ticket(home, key, title, ticket_args, actor="dispatcher", source="inbox/_new")
        minted.append(key)
        if dedup:
            minted_dedup[dedup] = key
            dedup_changed = True
    inbox.ack_new(home)
    if dedup_changed:
        store.write_json(minted_path, minted_dedup)
    return minted


def _would_mint_keys(home: Path) -> list[str]:
    """Read-only echo of what ``mint_new_tickets`` would create, for a preview's
    ``would_mint`` -- reads ``inbox/_new`` (via ``inbox.pending_new``, which does
    not drain it) and ``.schedule_minted.json``, mirrors the same dedup/existing-
    key skip rules, but writes nothing: no ``TicketCreated``, no cursor advance,
    no ``.schedule_minted.json`` update. Two un-keyed pending entries sharing a
    prefix would collide on the same auto-key if resolved independently (neither
    has created a ticket dir yet) -- ``claimed`` tracks this preview's own
    would-be keys so they don't stomp each other."""
    minted_dedup = store.read_json(home / "derived" / ".schedule_minted.json", {}) or {}
    would: list[str] = []
    claimed: set[str] = set()
    for _idx, entry in inbox.pending_new(home):
        ticket_args = entry.get("args") or {}
        dedup = ticket_args.get("dedup")
        if dedup and dedup in minted_dedup:
            continue  # already minted this period — guaranteed no-op
        key = entry.get("key")
        if not key:
            prefix = entry.get("prefix") or "T"
            n = 1
            while (home / "tickets" / f"{prefix}-{n}").exists() or f"{prefix}-{n}" in claimed:
                n += 1
            key = f"{prefix}-{n}"
        try:
            store.validate_key(key)
        except store.MaestroError:
            continue
        if event_log.last_seq(home, key) > 0:
            continue  # already has events -- not a fresh mint (see mint_new_tickets)
        claimed.add(key)
        would.append(key)
    return would


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
    priority = args.get("priority") or 3
    depends_on = args.get("depends_on") or []
    deps_str = ", ".join(depends_on) if depends_on else ""
    lines = [
        f"# {key}: {title}",
        "",
        "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->",
        "",
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
    if args.get("runner"):
        lines.append(f"runner: {args['runner']}")
    if args.get("runner_model"):
        lines.append(f"runner_model: {args['runner_model']}")
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
    """Opt-in external-tracker sync tick (e.g. Jira, Linear). No-op unless at least
    one tracker other than "none" is configured. Multiple trackers can be declared
    at once (``providers.get_trackers``) and run independently: each is gated by
    its own ``sync_interval`` via a persisted, per-name cursor (level-triggered,
    idempotent — matches ``mint_new_tickets``), and each ticket refreshes against
    the ONE tracker matching its own ``external_source`` — never another one's.
    Imports new work via ``import_new`` (which itself funnels through the audited
    ``_new`` inbox) and refreshes every tracked, not-done ticket sourced from a due
    tracker.
    """
    home = cfg.home
    # Import lazily to avoid a hard dependency from the core onto any one adapter.
    from . import providers

    trackers = providers.get_trackers(cfg)
    if not trackers:
        return {"imported": 0, "refreshed": 0}

    cursor_path = home / "derived" / ".sync_cursor.json"
    cursor = store.read_json(cursor_path, {}) or {}

    imported = 0
    due_names = []
    for name, tracker in trackers.items():
        settings = cfg.provider_config.get("tracker", {}).get(name, {})
        interval = int(settings.get("sync_interval", 900))
        if now - cursor.get(name, 0) < interval:
            continue
        due_names.append(name)
        imported += tracker.import_new(home)

    if not due_names:
        return {"imported": 0, "refreshed": 0}

    due = set(due_names)
    refreshed = 0
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        if snap.external_source not in due:
            continue
        if Phase(snap.phase) in TERMINAL_PHASES:
            continue
        if not snap.external_id:
            continue
        refreshed += trackers[snap.external_source].refresh(home, key, snap.external_id)
        if event_log.last_seq(home, key) > snap.observed_seq:
            snap_mod.rebuild(home, key)

    for name in due_names:
        cursor[name] = now
    store.write_json(cursor_path, cursor)
    return {"imported": imported, "refreshed": refreshed}


def _schedule_cursor_path(home: Path) -> Path:
    return home / "derived" / ".schedule_cursor.json"


def run_scheduled_tasks(cfg: Config, now: float) -> dict:
    """Config-declared recurring-trigger tick, mirroring ``sync_external_sources``:
    for each enabled+due ``[[scheduled]]`` task, mint one create-request into the
    ``_new`` inbox and advance a single ``derived/.schedule_cursor.json``
    ({name: last_fired_ts}). Level-triggered, not edge-accumulating: a task fires
    ONCE on the next sweep after a long downtime.

    Two cadence shapes (GA-19), both behind ``schedule.is_due``/``dedup_bucket``,
    so this function never branches on 'every' vs 'cron' for anything but the
    cursor-advance: an interval task's cursor snaps to the elapsed slot boundary
    (``schedule.advance_cursor``), so a late fire never drags its whole cadence
    forward with it; a cron task's cursor snaps to the exact matched wall-clock
    slot (``schedule.cron_due_slot``), which is what makes the DST fall-back rule
    (fires exactly once across the repeated hour) hold across sweeps.
    """
    home = cfg.home
    cursor_path = _schedule_cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    fired: list[str] = []
    for task in cfg.scheduled:
        if not task.get("enabled", True):
            continue
        name = task.get("name")
        if not name or not task.get("prompt") or not (task.get("every") or task.get("cron")):
            continue
        last = cursor.get(name, 0)
        if not schedule.is_due(task, last, now):
            continue
        args = {
            "intent": task["prompt"],
            "kind": task.get("kind", "implementation"),
            "priority": task.get("priority", 3),
            "scheduled_by": name,
            "dedup": f"{name}:{schedule.dedup_bucket(task, now)}",
        }
        for opt_field in schedule.OPTIONAL_MINT_FIELDS:
            val = task.get(opt_field)
            if val:
                args[opt_field] = val
        inbox.append_new(
            home,
            title=task.get("title") or name,
            prefix=task.get("prefix"),
            args=args,
        )
        if task.get("cron"):
            slot = schedule.cron_due_slot(task, last, now)
            cursor[name] = slot if slot is not None else now
        else:
            cursor[name] = schedule.advance_cursor(last, now, schedule.period(task))
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
        has_cadence = bool(task.get("every") or task.get("cron"))
        rows.append({
            "name": name,
            "prompt": task.get("prompt"),
            "every": task.get("every"),
            "cron": task.get("cron"),
            "tz": task.get("tz") or "UTC",
            "kind": task.get("kind", "implementation"),
            "priority": task.get("priority", 3),
            "prefix": task.get("prefix"),
            "enabled": enabled,
            "repo": task.get("repo"),
            "title": task.get("title"),
            "last_fired": last,
            "next_due": schedule.next_due(task, last or 0, now) if enabled and has_cadence else None,
        })
    return rows


_REPO_PREFLIGHT_SENTINELS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_REPO_PREFLIGHT_DIRS = ("rebase-merge", "rebase-apply")


def _probe_repo_path(repo: str) -> dict:
    """Read-only guard: is *repo* safe to fast-forward and spawn a reconciler
    into right now? Pure git probe -- no config knob, no fail-open-on-unset
    check (that's ``repo_preflight``/``repo_preflight_all``'s job); this is the
    part that's identical whether there is one repo or N.

    Three probes, all read-only, all against the SAME repo the dispatcher fast-
    forwards in ``sync_worktrees`` and spawns bare-repo reconcilers into (see
    ``_worker_cwd``): (a) an in-progress merge/rebase/cherry-pick/revert, via
    the sentinel files git leaves under the git dir; (b) unmerged index
    entries; (c) a tracked file carrying a REAL conflict hunk (both markers —
    a file that only quotes one, e.g. this feature's own tests/docs, must not
    block; see T-19 spec Notes on why ``--all-match`` is load-bearing).

    Fails OPEN — returns ``ok=True`` — on anything that makes the probe itself
    unusable (git missing, unborn HEAD, a hung subprocess): a flaky check that
    can permanently brick the fleet is a worse failure mode than the one this
    guards against, so only positive evidence of a real conflict blocks. Never
    raises into ``dispatch()``.
    """
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


def repo_preflight(cfg: Config) -> dict:
    """Back-compat single-repo verdict for ``cfg.repo_path`` only -- what
    ``maestro doctor``'s top-level ``repo_preflight`` field and the pre-MR-5
    test suite pin. Multi-repo sweeps use ``repo_preflight_all`` instead.
    """
    if not cfg.repo_preflight:
        return {"ok": True, "blockers": []}
    repo = cfg.repo_path
    if not repo or not Path(repo).exists():
        return {"ok": True, "blockers": ["preflight-unavailable"]}
    return _probe_repo_path(repo)


def referenced_repo_bindings(cfg: Config, home: Path, keys) -> dict:
    """{name: RepoBinding} for every repo *keys* resolve to, plus the implicit
    default (always included, even with an empty *keys* -- a 1-repo board
    still gets exactly one binding). Two names resolving to the same repo
    keep only the first-seen binding under each of their own names; callers
    that need to dedup probes by realpath do so themselves (see
    ``repo_preflight_all`` / ``sync_worktrees``, both of which already dedup
    fetches this way)."""
    from . import repos as repos_mod  # lazy: repos imports us at module load time

    bindings: dict = {}
    default = repos_mod.implicit_default(cfg)
    bindings[default.name] = default
    for key in keys:
        binding = repos_mod.resolve(cfg, home, key)
        bindings.setdefault(binding.name, binding)
    return bindings


def repo_preflight_all(cfg: Config, home: Path, keys) -> dict:
    """Per-repo preflight across every repo *keys* (typically the due tickets)
    resolve to, plus the implicit default — referenced-only, so a single-repo
    board still runs exactly one probe. Probes are deduped by realpath (two
    ``[repos.*]`` names pointing at one checkout cost one probe).

    Returns ``ok`` (no referenced repo is blocked), ``blockers_by_repo``
    ({name: [blockers]}, present only for genuinely blocked repos -- a repo
    that fails OPEN because it's unset/absent/unusable never appears here,
    exactly as a fail-open ``repo_preflight`` verdict never blocks a spawn
    today), and ``blockers`` (the flat, order-stable union list existing
    single-repo readers -- the heartbeat's ``repo_blockers`` key, ``cmd_dispatch``'s
    ``repo_blocked``, ``_nudge`` — already tolerate).
    """
    if not cfg.repo_preflight:
        return {"ok": True, "blockers_by_repo": {}, "blockers": []}
    bindings = referenced_repo_bindings(cfg, home, keys)
    verdict_by_realpath: dict[str, dict] = {}
    blockers_by_repo: dict[str, list[str]] = {}
    for name, binding in bindings.items():
        path = binding.path
        if not path or not Path(path).exists():
            continue  # fails OPEN, same as repo_preflight on an unusable repo
        real = str(Path(path).resolve())
        if real not in verdict_by_realpath:
            verdict_by_realpath[real] = _probe_repo_path(path)
        verdict = verdict_by_realpath[real]
        if not verdict["ok"]:
            blockers_by_repo[name] = verdict["blockers"]
    flat: list[str] = []
    for blockers in blockers_by_repo.values():
        for b in blockers:
            if b not in flat:
                flat.append(b)
    return {"ok": not blockers_by_repo, "blockers_by_repo": blockers_by_repo, "blockers": flat}


def _drift_policy_cursor_path(home: Path) -> Path:
    return home / "derived" / ".drift_policy_cursor.json"


def sync_worktrees(cfg: Config, now: float | None = None) -> dict:
    """Keep other tickets' worktrees from drifting behind a just-merged base branch.

    Groups every ticket's worktree by its *resolved* repo (``repos.resolve`` --
    unbound tickets and single-repo boards all resolve to the same implicit
    default, so this is byte-identical to single-repo behavior when no
    ``[repos.*]`` tables are configured), keyed on ``(realpath, base_branch)``
    so two config entries pointing at one checkout with the SAME base branch
    fetch only once, while a (contrived, but never silently wrong) checkout
    tracking two different base branches still gets both fetched correctly.
    Each repo is fetched against its OWN ``base_branch`` (fast-forwarding the
    local branch of that name too, when it's checked out) at most once per
    sweep; the implicit default repo is always included even with zero due
    tickets, so a single-repo board's local ``main`` still tracks
    ``origin/main`` every sweep. The fetch + local fast-forward always run,
    independent of ``base_drift_policy`` below -- they're cheap, useful on
    their own, and never coupled to whether drift alone is allowed to route.

    For every ticket sitting in ``awaiting-ci``/``in-review`` whose worktree is
    now behind its own repo's base, whether that drift alone routes it back
    into ``implementing`` (so the reconciler rebases, exactly as it already
    does for a GitHub-reported CONFLICTING PR -- see ``ops.route_conflict``)
    is gated on the resolved repo binding's ``base_drift_policy`` (MTO-2):
    ``"always"`` routes unconditionally (today's byte-identical behavior),
    ``"on_conflict"`` never routes on drift alone (only a CONFLICTING PR still
    does, via the separate ``sync_vcs`` hook), and ``"daily"`` routes at most
    once per calendar day per ticket, deduped via
    ``schedule.dedup_bucket``/a persisted cursor
    (``derived/.drift_policy_cursor.json``) keyed on ``(key, day-bucket)`` --
    reusing the same "have I done this today" machinery ``run_scheduled_tasks``
    already relies on, rather than hand-rolling a timestamp comparison. Under
    every policy, a ticket whose latest observed CI is still ``pending`` is
    never routed on drift alone -- staleness only matters at merge time, and
    routing while checks are still running is exactly what restarts CI and
    livelocks a fast-moving base (see the ticket's evidence). A ticket that's
    already ``implementing`` needs no nudge — it re-syncs with its base branch
    on every turn on its own. Suppressed reroutes (policy gate or pending CI)
    are reported under ``skipped_by_policy`` rather than silently absent.

    Level-triggered and idempotent: a no-op when nothing is behind, and one
    repo's fetch/network failure is contained to that repo -- it never stalls
    sibling repos' sync or this sweep's mint/backup ticks. ``fetched`` mirrors
    the *default* repo's fetch outcome only, keeping the historical
    single-repo return contract byte-identical; per-repo detail (including
    failures) rides the additional ``errors`` key.

    Each group is preflighted (``repo_preflight``/MR-5) before its fetch: a
    mid-merge/conflicted repo is skipped entirely for THIS group only (no
    fetch, no merge, no routing -- its ``FETCH_HEAD``/local branch are left
    untouched) while every other group's sync proceeds unaffected. Blocked
    groups land in the returned ``blocked`` map ({name: [blockers]}), never in
    ``errors`` (that key stays reserved for a real git/network failure).
    """
    import subprocess

    from . import ops
    from . import repos as repos_mod

    home = cfg.home
    now = now if now is not None else store.now_epoch()
    routed: list[str] = []
    skipped_by_policy: list[str] = []
    errors: dict[str, str] = {}
    blocked: dict[str, list[str]] = {}
    # Loaded/persisted lazily -- only "daily"-policy tickets that actually
    # drift ever touch this file, so a quiet sweep (or a board with no
    # "daily" repo) costs nothing.
    daily_cursor: dict = {}
    daily_cursor_loaded = False
    daily_cursor_changed = False

    # (realpath, base_branch) -> {"binding": RepoBinding, "keys": [...]}. The
    # implicit default repo is always a group (even with no due tickets) so
    # its local base branch still tracks origin every sweep, matching
    # historical behavior.
    groups: dict[tuple[str, str], dict] = {}

    def _group_for(binding) -> dict | None:
        if not binding.path or not Path(binding.path).exists():
            return None
        real = str(Path(binding.path).resolve())
        base = binding.base_branch or "main"
        return groups.setdefault((real, base), {"binding": binding, "keys": []})

    default_binding = repos_mod.implicit_default(cfg)
    default_gkey = None
    if default_binding.path and Path(default_binding.path).exists():
        default_gkey = (str(Path(default_binding.path).resolve()),
                        default_binding.base_branch or "main")
        _group_for(default_binding)

    for key in list_keys(home):
        wt = store.worktree_path(home, key)
        if not wt.exists():
            continue
        snap = snap_mod.load(home, key)
        if Phase(snap.phase) not in (Phase.AWAITING_CI, Phase.IN_REVIEW):
            continue
        binding = repos_mod.resolve(cfg, home, key)
        group = _group_for(binding)
        if group is None:
            continue
        group["keys"].append(key)

    fetched_default = False
    for gkey, group in groups.items():
        real, base = gkey
        binding = group["binding"]
        repo_path = binding.path
        if cfg.repo_preflight:
            verdict = _probe_repo_path(repo_path)
            if not verdict["ok"]:
                blocked[binding.name] = verdict["blockers"]
                continue  # this group only -- siblings still fetch/route below
        try:
            fetch = subprocess.run(["git", "-C", repo_path, "fetch", "-q", "origin", base],
                                   capture_output=True, text=True)
            if fetch.returncode != 0:
                errors[f"{real}@{base}"] = (fetch.stderr or "fetch failed").strip()
                continue
            if gkey == default_gkey:
                fetched_default = True
            # only fast-forward the shared checkout's own HEAD -- `--ff-only` operates on
            # whatever branch is checked out, so guard it explicitly rather than relying on
            # base not being checked out; a detached HEAD (symbolic-ref fails) also no-ops
            head = subprocess.run(["git", "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
                                  capture_output=True, text=True)
            if head.returncode == 0 and head.stdout.strip() == base:
                subprocess.run(["git", "-C", repo_path, "merge", "-q", "--ff-only", f"origin/{base}"],
                               capture_output=True, text=True)

            for key in group["keys"]:
                wt = store.worktree_path(home, key)
                behind = subprocess.run(
                    ["git", "-C", str(wt), "rev-list", "--count", f"HEAD..origin/{base}"],
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

                policy = binding.base_drift_policy
                if policy == "on_conflict":
                    skipped_by_policy.append(key)
                    continue
                # Staleness only matters at merge time -- never restart CI that's
                # still running, under ANY policy (this is what actually breaks
                # the CI-restart livelock; see the ticket's evidence).
                if snap_mod.load(home, key).ci_state == "pending":
                    skipped_by_policy.append(key)
                    continue
                if policy == "daily":
                    if not daily_cursor_loaded:
                        daily_cursor = store.read_json(_drift_policy_cursor_path(home), {}) or {}
                        daily_cursor_loaded = True
                    bucket = schedule.dedup_bucket({"every": "24h"}, now)
                    if daily_cursor.get(key) == bucket:
                        skipped_by_policy.append(key)
                        continue
                    if ops.route_stale(cfg, key, base_branch=base, policy=policy):
                        routed.append(key)
                        daily_cursor[key] = bucket
                        daily_cursor_changed = True
                    continue
                # policy == "always"
                if ops.route_stale(cfg, key, base_branch=base, policy=policy):
                    routed.append(key)
        except Exception as e:  # noqa: BLE001 - one repo's failure must never stall siblings
            errors[f"{real}@{base}"] = f"{type(e).__name__}: {e}"
            continue

    if daily_cursor_changed:
        store.write_json(_drift_policy_cursor_path(home), daily_cursor)

    return {"fetched": fetched_default, "routed": routed,
            "skipped_by_policy": skipped_by_policy, "errors": errors, "blocked": blocked}


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
    worktree_removal_errors: dict[str, str] = {}
    # GA-17: memoized per (gh_account, token_env) for this whole sync tick.
    credential_cache: dict = {}
    for key in list_keys(home):
        snap = snap_mod.load(home, key)
        phase = Phase(snap.phase)
        if phase not in (Phase.AWAITING_CI, Phase.IN_REVIEW) or not snap.pr_number:
            continue
        checked += 1
        repo_slug = repos.resolve_vcs_slug(cfg, snap)
        binding = repos.resolve(cfg, home, key)
        cred = resolve_credential(binding, credential_cache)
        if not cred.ok:
            # Fail closed (GA-17): never let the dispatcher's own gh poll fall
            # back to the ambient account. Synthesize the same "auth" shape a
            # real `gh` credential failure would produce (see
            # providers/cli.py's classify_gh_failure) so _observe_ci's existing
            # auth-failure routing (a visible ops.fail naming the repo/PR)
            # handles this identically -- no second failure path to invent.
            status = {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                     "ci_state": "unknown", "failing_checks": [], "error": "auth"}
        else:
            status = vcs.pr_status(snap.pr_number, repo=repo_slug, env=cred.env)

        try:
            if _route_if_merged(cfg, key, status, worktree_removal_errors):
                continue
            if status.get("mergeable") == "CONFLICTING":
                from . import ops
                ops.route_conflict(cfg, key, snap.pr_number, actor="dispatcher")
                continue

            _observe_ci(cfg, key, status, phase, repo_slug=repo_slug, pr_number=snap.pr_number)
            if cred.ok:
                _observe_reviews(cfg, key, snap.pr_number, vcs, repo=repo_slug, env=cred.env)
        except event_log.StaleAppendError:
            # Lost the fencing race against a concurrent writer (a human, a
            # reconciler, or another dispatcher tick) that appended between
            # this loop's fold and here -- benign, no failure_count spend, no
            # partial routing for this key. The next sweep re-derives it from
            # the now-current log, same as any other ticket this tick skipped.
            continue

    cursor[vcs_name] = now
    store.write_json(cursor_path, cursor)
    return {"checked": checked, "worktree_removal_errors": worktree_removal_errors}


def _route_if_merged(cfg: Config, key: str, status: dict,
                     worktree_removal_errors: dict | None = None) -> bool:
    from . import ops
    from . import repos as repos_mod
    if not ops.check_merged(cfg, key, status.get("state", ""), actor="dispatcher"):
        return False
    import subprocess
    wt = store.worktree_path(cfg.home, key)
    if wt.exists():
        # Removal must run in the ticket's OWNING repo -- `git -C <worktree>
        # worktree remove` does not work; the repo's own git metadata is what
        # tracks the worktree registration.
        binding = repos_mod.resolve(cfg, cfg.home, key)
        if binding.path:
            result = subprocess.run(
                ["git", "-C", binding.path, "worktree", "remove", str(wt), "--force"],
                capture_output=True, text=True,
            )
            if result.returncode != 0 and worktree_removal_errors is not None:
                worktree_removal_errors[key] = (result.stderr or result.stdout
                                                or "worktree remove failed").strip()
    return True


def _observe_ci(cfg: Config, key: str, status: dict, phase: Phase, *,
                repo_slug: str | None = None, pr_number: int | None = None) -> None:
    error = status.get("error")
    if error == "transient":
        # A retryable gh-side blip (timeout, network hiccup): not observed at
        # all. No event, no phase change, no failure_count spend, and — since
        # nothing is appended — no risk of overwriting an already-known
        # ci_state with "unknown". Free retry on the next poll.
        return

    ci_state = status.get("ci_state", "unknown")
    failing = sorted(status.get("failing_checks") or [])
    head_sha = status.get("head_sha") or "unknown"
    # `error` participates in the check-key so a genuine auth/not_found result
    # is never deduped against a pre-existing bare {"state": "unknown"}
    # CiObserved (minted before this classification existed, or by an
    # "unknown"-class result) — it always hashes differently and so always
    # gets a fresh event to route on. Repeats of the SAME error still dedupe
    # by construction (same hash in -> same step-id -> `ops.fail` runs once).
    check_key = content_hash(ci_state + ":" + ",".join(failing) + ":" + (error or ""))
    sid = f"ci-{key}-{head_sha}-{check_key}"
    detail = f"{len(failing)} check(s) failing: {', '.join(failing)}" if failing else ""
    payload = {"state": ci_state, "failing_checks": failing, "detail": detail}
    if error:
        payload["error"] = error
    ev = event_log.append(cfg.home, key, E.CI_OBSERVED, payload,
                          actor="dispatcher", step_id=sid)
    if ev is None:
        return  # unchanged since the last poll — nothing to route
    fresh = snap_mod.rebuild(cfg.home, key)
    from . import ops
    if error in ("auth", "not_found"):
        where = f"{repo_slug or '?'}#{pr_number if pr_number is not None else '?'}"
        ops.fail(cfg, key, f"gh {error}: could not read PR {where}", actor="dispatcher")
    elif ci_state == "failing":
        ops.set_phase(cfg, key, Phase.IMPLEMENTING,
                      reason=f"CI failing: {', '.join(failing)}", actor="dispatcher",
                      expect=fresh.observed_seq)
    elif ci_state == "passing" and phase == Phase.AWAITING_CI:
        ops.set_phase(cfg, key, Phase.IN_REVIEW, reason="CI passing", actor="dispatcher",
                      expect=fresh.observed_seq)


def _observe_reviews(cfg: Config, key: str, pr_number: int, vcs, repo: str | None = None,
                     env: dict | None = None) -> None:
    changes_requested_body: str | None = None
    for r in vcs.review_feedback(pr_number, repo=repo, env=env):
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
    fresh = snap_mod.rebuild(cfg.home, key)
    from . import ops
    ops.set_phase(cfg, key, Phase.IMPLEMENTING,
                 reason=f"changes requested: {changes_requested_body}", actor="dispatcher",
                 expect=fresh.observed_seq)


# RB-14: board-wide cap on IN-FLIGHT dispatcher-owned test-run subprocesses --
# mirrors `_RUNNER_DEFAULT_CONCURRENCY`'s shape (a small, explicit, documented
# ceiling below), but is its own knob: a test run costs CPU/IO, never a token,
# so it has nothing to do with `max_concurrency` (agent sessions) or any
# per-runner LLM cap. A plain module constant, not per-repo config -- nothing
# here has ever needed per-repo tuning; raise it in code if that changes.
TEST_RUN_CONCURRENCY = 4


def _test_run_dir(home: Path, key: str) -> Path:
    return home / "derived" / "testruns" / store.validate_key(key)


def _test_run_result_path(home: Path, key: str) -> Path:
    """Where a detached test-run subprocess (`_start_test_run`) writes its own
    exit code once done -- the only way its result survives past THIS
    `dispatch()` process exiting (see `_start_test_run`'s docstring)."""
    return _test_run_dir(home, key) / "result"


def _test_run_log_path(home: Path, key: str) -> Path:
    return _test_run_dir(home, key) / "output.log"


def sync_test_runs(cfg: Config, now: float) -> dict:
    """RB-14: dispatcher-owned test verification -- the `sync_vcs` pattern
    (poll/advance a sleeping phase directly, no reconciler spawn) applied to
    `cfg.test_command` instead of a PR. For every `verifying` ticket: start its
    test run as a tracked, detached subprocess if none is in flight yet, or
    fold one that has already finished and route the phase on (`qa` on a pass;
    back to `implementing`, with a bounded failure excerpt, on a fail).

    Never blocks: a started run is left running (`start_new_session=True`,
    exactly like `sessions.ClaudeCliSessions.spawn`) and this function returns
    immediately either way -- a still-running key is simply left for a later
    sweep to fold. The in-flight run holds *key*'s existing per-ticket claim
    (`claims.write_claim(..., kind="testrun")`) -- the SAME runner-agnostic
    liveness/dedup machinery a reconciler session's claim already uses
    (RF-2's Notes: `claims._verdict` keys only on pid + `start_epoch`, never
    the spawned command string), not a second mechanism.

    No-op (`{"checked": 0}`) when `test_command` is unset -- ships dark, same
    posture as every other RB-12/RB-14 gate.
    """
    home = cfg.home
    if not cfg.test_command:
        return {"checked": 0}
    from . import ops, repos as repos_mod

    checked = 0
    started: list[str] = []
    folded: list[str] = []
    # T-52: the same race `_spawn_lock_target` exists to close -- two
    # concurrent `dispatch()` calls (a launchd tick racing `cli._nudge`) must
    # not both decide the same key's claim is free and both Popen a test run
    # for it. Held for this whole sweep's worth of verifying keys, mirroring
    # the real spawn region's own single-lock-for-the-region shape; this
    # function always runs (and fully returns) before `dispatch()`'s own
    # `with store.file_lock(_spawn_lock_target(home)):` block, so there is no
    # nesting/deadlock risk.
    with store.file_lock(_spawn_lock_target(home)):
        for key in list_keys(home):
            snap = snap_mod.load(home, key)
            if Phase(snap.phase) != Phase.VERIFYING:
                continue
            checked += 1
            claim = claims.read_claim(home, key)
            if claim is not None and claim.get("kind") == "testrun":
                if claims.pid_alive(claim.get("pid")):
                    continue  # in flight -- fold on a later sweep
                _fold_test_run(cfg, key, claim)
                folded.append(key)
                continue
            if claim is not None:
                continue  # some other claim still holds this key -- leave it alone
            binding = repos_mod.resolve(cfg, home, key)
            if binding.mode == "local":
                # Should never route here (the set_phase redirect excludes
                # mode:local), but never wedge a ticket that somehow did.
                ops.set_phase(cfg, key, Phase.QA, reason="mode:local, no suite to run",
                              actor="dispatcher")
                continue
            cwd = _worker_cwd(cfg, key)
            tree_key = ops._tree_state_key(cwd)
            cached = snap.test_runs.get(tree_key)
            if cached is not None:
                _route_test_run(cfg, key, cached, actor="dispatcher")
                continue
            in_flight = sum(
                1 for c in claims.all_claims(home).values()
                if c.get("kind") == "testrun" and claims.pid_alive(c.get("pid")))
            if in_flight >= TEST_RUN_CONCURRENCY:
                continue  # at the board-wide cap -- retried next sweep
            _start_test_run(home, key, cwd, cfg.test_command)
            started.append(key)
    return {"checked": checked, "started": started, "folded": folded}


def _start_test_run(home: Path, key: str, cwd: Path, command: str) -> None:
    """Launch *command* for *key* as a detached, tracked subprocess -- the
    Popen half of `ops.capture_tests`'s own blocking `subprocess.run`, made
    non-blocking. Wrapped in a shell so the CHILD, not this (about-to-exit)
    `dispatch()` process, writes its own exit code once done: a detached
    (`start_new_session=True`) child is reparented to init the moment this
    process exits, so a LATER `maestro dispatch` invocation is never its
    parent and can't `waitpid()` it -- it can only read what the child left
    behind for itself (`_test_run_result_path`/`_test_run_log_path`)."""
    import shlex
    import subprocess

    result_path = _test_run_result_path(home, key)
    log_path = _test_run_log_path(home, key)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    tmp_result = result_path.with_suffix(".tmp")
    wrapped = (f"{command} > {shlex.quote(str(log_path))} 2>&1; "
              f"echo $? > {shlex.quote(str(tmp_result))} && "
              f"mv {shlex.quote(str(tmp_result))} {shlex.quote(str(result_path))}")
    # T-52's own reservation order: write the claim with THIS process's own
    # pid (self-healing the instant this process dies too, before Popen even
    # runs) first, then overwrite with the real child pid -- see
    # `sessions.ClaudeCliSessions.spawn`'s identical comment; claims are
    # runner-agnostic (RF-2), so a bare subprocess needs the exact same fix,
    # not a second one.
    claims.write_claim(home, key, os.getpid(), f"testrun-{key}",
                       cwd=str(cwd), kind="testrun")
    try:
        proc = subprocess.Popen(["sh", "-c", wrapped], cwd=str(cwd),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception:
        claims.release(home, key)
        raise
    claims.write_claim(home, key, proc.pid, f"testrun-{key}",
                       cwd=str(cwd), kind="testrun")


def _fold_test_run(cfg: Config, key: str, claim: dict) -> None:
    """Read back a finished test-run subprocess's result and route the phase
    on it. A missing/unparsable result (the wrapper shell itself was killed,
    or crashed, before it could write one) folds as a failure -- never wedges
    the ticket in `verifying` forever over a process that's already gone."""
    home = cfg.home
    result_path = _test_run_result_path(home, key)
    log_path = _test_run_log_path(home, key)
    claims.release(home, key)
    cwd = claim.get("cwd") or str(_worker_cwd(cfg, key))
    from . import ops

    tree_key = ops._tree_state_key(Path(cwd))
    try:
        exit_code = int(result_path.read_text(encoding="utf-8").strip())
        output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    except (OSError, ValueError):
        exit_code = -1
        output = "[maestro] test-run process left no result -- treated as a failure"
    result_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    payload = ops._record_test_run(cfg, key, tree_key=tree_key, command=cfg.test_command,
                                   exit_code=exit_code, output=output, actor="dispatcher")
    _route_test_run(cfg, key, payload, actor="dispatcher")


def _route_test_run(cfg: Config, key: str, record: dict, *, actor: str) -> None:
    """Advance *key* off `verifying` per a captured test-run record -- the
    routing half both the freshly-folded and the tree-cache-reuse paths in
    `sync_test_runs` share, so the two can never route the same shape of
    record differently."""
    from . import ops

    fresh = snap_mod.rebuild(cfg.home, key)
    if record.get("passed"):
        ops.set_phase(cfg, key, Phase.QA, reason="tests passed", actor=actor,
                      expect=fresh.observed_seq)
        return
    excerpt = record.get("failure_excerpt") or ""
    reason = f"tests failed (exit {record.get('exit_code')})"
    if excerpt:
        reason += f": {excerpt}"
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason=reason, actor=actor,
                  expect=fresh.observed_seq)


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


def _killpg_best_effort(pid) -> None:
    """Best-effort SIGTERM to the process group (pid == pgid, since sessions
    spawn with ``start_new_session=True``) -- a pid that's already gone is not
    an error, just one less thing to kill. The default killer; tests inject
    their own to assert *what* would be killed without a real process."""
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError, TypeError, OSError):
        pass


def run_watchdog(cfg: Config, now: float, *, kill=None) -> list[str]:
    """Reap a claim that is either too OLD (``max_session_seconds``, 0 disables)
    or has gone SILENT (``no_output_timeout``, 0 disables). ``claims.active_keys``
    only checks pid-alive, so a live-but-stuck session -- wedged at startup with
    no output, no error, no exit -- holds its key and a concurrency slot forever;
    this closes that gap. Both clocks are independent: a fresh log survives past
    ``max_session_seconds``, and a stale log is reaped even with a young claim
    epoch. Must run before ``active`` is computed so a reaped claim never counts
    toward concurrency. *kill* defaults to a best-effort SIGTERM-to-pgid
    (``_killpg_best_effort``); injectable so a test can assert the pid passed to
    it without spawning a real process.

    The no-output check is exempt for a claim recorded without ``log_path``
    (``capture_session_logs = false`` -- no output signal, never reap on missing
    data) -- it still falls through to the age-based check below.
    """
    home = cfg.home
    max_seconds = cfg.max_session_seconds
    no_output_timeout = cfg.no_output_timeout
    kill = kill or _killpg_best_effort
    if not max_seconds and not no_output_timeout:
        return []
    reaped: list[str] = []
    for key, claim in claims.all_claims(home).items():
        reason = None
        log_path = claim.get("log_path")
        if no_output_timeout and log_path:
            try:
                mtime = os.stat(log_path).st_mtime
            except OSError:
                mtime = None
            if mtime is not None and now - mtime > no_output_timeout:
                reason = f"no output for over {no_output_timeout}s (log: {log_path})"
        if reason is None:
            epoch = claim.get("epoch")
            if (max_seconds and isinstance(epoch, (int, float))
                    and now - epoch > max_seconds):
                reason = f"session exceeded {max_seconds}s"
        if reason is None:
            continue
        pid = claim.get("pid")
        kill(pid)
        claims.release(home, key)
        from . import ops
        ops.fail(cfg, key, f"watchdog: {reason} (pid {pid})", actor="dispatcher")
        reaped.append(key)
    return reaped


def detect_zero_turn_spawns(cfg: Config, now: float) -> list[str]:
    """Fail, on sight, any dead claim whose session's terminal ``result`` shows
    ``num_turns == 0`` (T-45) -- e.g. a resolved reconcile command that doesn't
    exist in the session's cwd: ``claude -p`` returns in ~24ms with
    ``result: "Unknown command: ..."`` before any tool could run, so no event
    could possibly have been appended. That is a structural failure the FIRST
    time it happens; left alone, ``active_keys()`` (called right after this,
    to compute ``active``) would silently release the claim as just another
    dead session and the ticket would simply respawn next sweep, forever. This
    is THE runaway net (T-58's spec: "without it there is none") -- it also
    sees a pi ``.pi.jsonl`` log (``steplog._pi_session_outcome`` synthesizes
    the same ``result.num_turns`` shape off pi's own ``turn_start`` count), so
    a board running the ``pi`` runner is not invisible to it.

    Must run before ``active = sessions.list_active()`` -- once that call
    releases a stale claim (its normal job), the ``log_path``/``cwd``/
    ``prompt`` this needs are gone. A claim whose pid is still alive, whose
    log is missing/not-yet-terminal, or whose session ran >=1 turn is left
    completely untouched -- that's the existing no-progress watchdog's
    territory (``_allow_spawn``/``max_spawn_attempts``), not this one's: a
    session that runs but appends nothing still needs N sweeps of that
    counter, unchanged.
    """
    home = cfg.home
    failed: list[str] = []
    for key, claim in claims.all_claims(home).items():
        if claims.pid_alive(claim.get("pid")):
            continue
        log_path = claim.get("log_path")
        if not log_path:
            continue
        p = Path(log_path)
        if not p.exists() or not (p.name.endswith(".stream.jsonl") or p.name.endswith(".pi.jsonl")):
            continue
        result = steplog.session_outcome(p).get("result")
        if not result or result.get("num_turns") != 0:
            continue  # no terminal result yet, or it ran real turns
        claims.release(home, key)
        from . import ops
        command = claim.get("prompt", "?")
        cwd = claim.get("cwd", "?")
        message = result.get("result", "")
        # Dead-letter on this, the first, offence -- a missing/broken reconcile
        # command will not fix itself between sweeps, so backing off and
        # retrying (ops.fail's default) would just respawn into the same
        # 0-turn death; see T-45's spec Notes for the retry-vs-dead-letter call.
        ops.fail(cfg, key,
                f"0-turn spawn: reconcile command {command!r} in cwd {cwd!r} exited "
                f"before any tool ran ({message!r})",
                actor="dispatcher", dead_letter=True)
        failed.append(key)
    return failed


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
    spend_ceiling_reason: str | None = None  # GA-11: set when the daily spend gate blocked this sweep
    reaped: list[str] = field(default_factory=list)     # watchdog: killed for age or no-progress
    paused: bool = False            # the fleet.pause() kill switch was armed this sweep
    repo_blockers: list[str] = field(default_factory=list)  # flat union; non-empty -> >=1 repo blocked
    repo_blockers_by_repo: dict = field(default_factory=dict)  # {repo name: [blockers]}, MR-5
    runner_blockers: dict = field(default_factory=dict)  # OC-2: {runner name: reason}, transient-preflight skip
    hook_errors: dict = field(default_factory=dict)     # hook name -> "ExcType: message"
    worktree_removal_errors: dict = field(default_factory=dict)  # key -> git worktree remove stderr
    would_mint: list[str] = field(default_factory=list)  # dry_run only: pending_new keys, unminted


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


def spawn_weight(cfg: Config, phase: str) -> int:
    """Expected-cost weight, in agent-equivalents, of ONE spawn of a ticket
    currently in *phase* (GA-14, retopologized by RF-7).

    AD-4/T-23's Implementer<->QA loop used to run entirely inside ONE
    `implementing` session, via `Agent`-tool sub-agent spawns -- up to
    `max_impl_turns` rounds, each firing a QA sub-agent the dispatcher's own
    spawn ledger could never see. RF-7 moved that adversarial check into its
    own dispatcher-spawned `qa` phase: each QA round is now a REAL spawn the
    ledger already counts on its own, so `implementing` no longer needs a
    preventive multiplier -- it fans out no sub-agents and weighs 1, like any
    other ordinary session.

    `qa`'s spec-axis judgment is the top-level `qa` session itself (structurally
    independent already, since `phase_denylist` denies it Edit/Write) -- no
    sub-agent there either. The Standards axis (T-23) is the one fan-out that
    stayed in-session, now inside `qa` rather than `implementing`: one extra
    `Agent`-tool sub-agent per `qa` spawn when `qa_standards_axis` is on. So
    `qa` weighs 1, or 2 with the Standards axis enabled -- one legal fan-out
    level, not a second dispatcher spawn."""
    if phase != Phase.QA.value:
        return 1
    return 1 + (1 if cfg.qa_standards_axis else 0)


def _ledger_entry_ts(entry) -> float | None:
    """One `recent` entry is either a bare timestamp (pre-GA-14 ledger, always
    weight 1) or a `[timestamp, weight]` pair (GA-14+). Returns the timestamp,
    or None if the entry is neither (a corrupt/foreign value -- never crash the
    dispatcher over a malformed ledger, just drop it)."""
    if isinstance(entry, (int, float)):
        return float(entry)
    if (isinstance(entry, (list, tuple)) and len(entry) == 2
            and isinstance(entry[0], (int, float))):
        return float(entry[0])
    return None


def _ledger_entry_weight(entry) -> int:
    """The agent-equivalent weight of one `recent` entry -- 1 for a legacy
    (pre-GA-14) bare-timestamp entry, since it predates weighting and so is
    read the same as an ordinary (non-`implementing`) session."""
    if (isinstance(entry, (list, tuple)) and len(entry) == 2
            and isinstance(entry[1], (int, float))):
        return int(entry[1])
    return 1


def _runaway_brake_state_path(home: Path) -> Path:
    return home / "derived" / ".runaway_brake.json"


def _maybe_trip_runaway_brake(cfg: Config, home: Path, now: float, health_mod) -> str | None:
    """GA-5: the auto-brake beside G4 (the ratelimit gate below). Arms
    ``fleet.pause`` on exactly the signal ``maestro doctor``'s ``runaway`` field
    reports -- ``health.spawn_rate(home, now)["total"] > health.spawn_budget(cfg)``
    and nothing else -- so doctor's verdict and the brake's verdict always agree
    (no human-signal exclusion, no second threshold). Called only when no
    fleet-wide pause is already active (G1 already returned early otherwise) and
    never during ``dry_run`` (arming a pause is a write, like ``ratelimit.probe``,
    which ``dry_run`` also skips).

    ``runaway_pause_cooldown == 0`` disables the auto-brake while leaving
    doctor's advisory intact; ``runaway_spawns_per_hour == 0`` disables both
    (``spawn_budget()`` returns 0, mirrored from ``health.report()``'s own
    ``bool(budget)`` guard).

    The resume wedge: ``health.spawn_rate`` reads a trailing ``WINDOW_SECONDS ==
    3600`` window, which almost always still exceeds the budget the moment a
    pause ends -- whether it expired naturally (G1's ``fleet.pause_state``
    auto-unlinks an elapsed ``until``) or was lifted early via ``maestro fleet
    resume`` (which touches neither this state nor the spawn ledger). A naive
    re-check right then re-arms on the spot, so the "next sweep spawns
    normally" AC would never actually hold and a human resume would be undone
    one sweep later. Fixed with our own small persisted marker
    (``derived/.runaway_brake.json``, holding the LAST armed ``until``):
    suppressed until ``prior_until + cooldown``, regardless of how that prior
    pause ended. Bounded and self-healing either way -- if the rate is still
    over budget once the grace period itself elapses, the brake fires again,
    exactly as it should for a genuinely still-runaway board.

    Returns the reason string if it armed a NEW pause this sweep, else ``None``.
    """
    cooldown = cfg.runaway_pause_cooldown
    if not cooldown:
        return None
    budget = health_mod.spawn_budget(cfg)
    if not budget:
        return None
    total = health_mod.spawn_rate(home, now)["total"]
    if total <= budget:
        return None
    state_path = _runaway_brake_state_path(home)
    prior = store.read_json(state_path, None)
    prior_until = prior.get("until") if isinstance(prior, dict) else None
    if isinstance(prior_until, (int, float)) and now < prior_until + cooldown:
        return None
    until = now + cooldown
    reason = (f"auto-pause: runaway spawn rate {total}/hour exceeds "
              f"budget {budget}/hour")
    fleet.pause(home, until=until, reason=reason)
    store.write_json(state_path, {"until": until, "armed_at": now})
    return reason


def _load_and_refresh_snapshot(home: Path, key: str) -> "snap_mod.Snapshot":
    """Load *key*'s persisted snapshot, refolding if a writer got ahead of it.

    Pulled out of the per-key sweep loop so `_run_hook` can wrap the whole
    load-then-maybe-refold step atomically per ticket (RB-2) -- a fold that
    somehow still raises must not abort the rest of the sweep.
    """
    snap = snap_mod.load(home, key)
    if event_log.last_seq(home, key) > snap.observed_seq:
        snap = snap_mod.rebuild(home, key)
    return snap


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


def dispatch(cfg: Config, sessions: SessionManager, now: float, dry_run: bool = False, *,
             runner_probe: Callable[[str], dict] | None = None,
             runner_verdict: Callable[[str], Callable] | None = None,
             key_filter: Iterable[str] | None = None) -> DispatchReport:
    """One sweep. ``sessions`` decides whether spawns actually launch (use
    ``DryRunSessions`` to record-without-launch). Always idempotent and safe to
    run on a timer — minting and folding are no-ops when nothing changed.

    ``runner_probe`` (OC-2) overrides ``_default_runner_probe`` for the
    non-claude runner preflight below -- tests inject a fake to assert every
    transient/permanent branch without a real binary or ollama daemon; real
    callers never pass it.

    ``runner_verdict`` (PI-3) overrides ``_make_default_runner_verdict(cfg)``
    for the permanent model-verdict branch below -- given a runner name, it
    returns the ``(models, daemon_reason, runner_model) -> (verdict, reason)``
    function to judge that runner's catalogue with. Tests inject a one-arg
    fake (``lambda runner: fake_verdict_fn``) to assert the ``ops.ask`` text
    comes from a runner-specific reason without a real ollama daemon; real
    callers never pass it.

    ``key_filter`` (MTO-4) restricts the candidate set to exactly these keys,
    resolved before due-checking so everything downstream -- due-checking,
    throttling, claims, the spawn ledger -- behaves normally but only ever
    considers them. An unknown key raises ``MaestroError`` (checked up front,
    before the fleet-pause gate, so a typo is never swallowed by a paused
    board). Minting is skipped entirely (the ``_new`` inbox holds keyless
    create-requests, so there is no way to scope it to a key that doesn't
    exist yet) -- a ``--key`` sweep drives an already-minted ticket, never
    creates one. Because the candidate set is restricted before the throttle
    check, a throttled target simply has no other candidate to fall back to:
    it idles rather than substituting a different key -- the substitution
    that makes an unrestricted sweep spawn a different due key when the
    floored one is skipped still applies exactly as before when
    ``key_filter`` is ``None``.

    ``dry_run`` (GA-4) makes the sweep strictly read-only: no ``TicketCreated``
    (``would_mint`` reports what mint_new_tickets would have minted, read via
    ``inbox.pending_new`` without draining it), no external-source sync, no
    scheduled-task fire, no worktree/VCS sync, no backup, no compact/archive/
    prune, no rate-limit or spend probe, no notify, no watchdog reap, and no spawn-ledger
    or spawn-attempts write (so it can never trip ``max_spawn_attempts`` into a
    real ``Failed``/``RequeueScheduled``, and never throttles the next real
    sweep). ``dry_run`` is an explicit, separate flag from the ``sessions``
    argument -- NEVER infer it from the ``SessionManager`` type: existing direct
    ``dispatch(cfg, DryRunSessions(), now=...)`` callers (most of this test
    suite) keep getting a full real sweep, only ``sessions.spawn()`` is a no-op.
    Only the snapshot refold and ``derived/*`` dashboards (heartbeat, the
    per-sweep decision ledger) are written either way -- both idempotent/
    regenerable, never the sole source of truth.
    """
    home = cfg.home

    filter_keys: frozenset[str] | None = None
    if key_filter is not None:
        filter_keys = frozenset(key_filter)
        unknown = set(k for k in filter_keys if k not in set(list_keys(home)))
        # RB-17: a key can be a `list_keys` member -- some derived-only
        # footprint on disk, e.g. a stray leftover snapshot -- while still
        # never having been minted (no spec.md, no ticket dir, no event-log
        # history). Surfaced as the same clear, attributable error as an
        # outright-unknown key (AC3) rather than let dispatch attempt it.
        unknown |= set(k for k in filter_keys if k not in unknown and _never_minted(home, k))
        if unknown:
            raise store.MaestroError(
                f"dispatch --key: unknown ticket key(s): {', '.join(sorted(unknown))}")

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

    from . import repos as repos_mod  # lazy: repos imports us at module load time

    hook_errors: dict = {}
    runner_blockers: dict = {}  # OC-2: {runner name: reason}, populated below only for a real spawn loop
    if dry_run:
        # Strictly read-only (GA-4 option (a)): none of the twelve hooks below
        # run -- each one either mutates the event log, drains/advances a
        # cursor, or touches the outside world (network, worktrees, backup
        # tarballs, notifications). `would_mint` is the one hook-shaped value a
        # preview still owes the caller, computed by reading (not draining)
        # inbox/_new instead of calling mint_new_tickets.
        minted: list[str] = []
        would_mint = [] if filter_keys is not None else _would_mint_keys(home)
        scheduled_fired: list[str] = []
        worktree_removal_errors: dict = {}
        reaped: list[str] = []
        pruned_logs = 0
        pruned_bytes = 0
        errors: dict = {}
    else:
        # MTO-4: a --key sweep never mints -- the _new inbox is keyless (a
        # request may not even carry the target key yet), so there is no way
        # to scope draining it to just `filter_keys` without either minting
        # an unrelated pending request or silently ack-ing it away unminted.
        # Left to the next unrestricted sweep instead.
        minted = ([] if filter_keys is not None else
                  _run_hook("mint_new_tickets", hook_errors, mint_new_tickets, cfg, default=[]))
        would_mint = []
        _run_hook("sync_external_sources", hook_errors, sync_external_sources, cfg, now)
        scheduled_fired = _run_hook("run_scheduled_tasks", hook_errors, run_scheduled_tasks,
                                    cfg, now, default={"fired": []})["fired"]
        # sync_worktrees preflight-gates itself per repo group now (MR-5) -- no
        # outer repo_ok gate needed here; a blocked repo skips only its own group.
        _run_hook("sync_worktrees", hook_errors, sync_worktrees, cfg, now)
        vcs_sync_result = _run_hook("sync_vcs", hook_errors, sync_vcs, cfg, now, default={})
        worktree_removal_errors = (vcs_sync_result or {}).get("worktree_removal_errors", {})
        # RB-14: same sleeping-phase-observation shape as sync_vcs just above,
        # applied to `verifying`/`cfg.test_command` instead of a PR.
        _run_hook("sync_test_runs", hook_errors, sync_test_runs, cfg, now, default={})
        _run_hook("backup", hook_errors, backup.maybe_backup, cfg, now)
        _run_hook("compact_tick", hook_errors, run_compact_tick, cfg, now)
        _run_hook("archive_tick", hook_errors, run_archive_tick, cfg, now)
        _run_hook("ratelimit_probe", hook_errors, ratelimit.probe, cfg, now)
        _run_hook("spend_probe", hook_errors, spend.probe, cfg, now)
        _run_hook("notify", hook_errors, notify.maybe_notify, cfg, now)

        # Watchdog runs before `active` is computed: a hung claim must not count
        # toward concurrency, and a reaped key needs to be re-fold-visible (fail
        # appends an event) before this sweep's due-check reads its snapshot.
        reaped = _run_hook("watchdog", hook_errors, run_watchdog, cfg, now, default=[])
        # T-45: same ordering requirement -- must run before `active_keys()`
        # (below) silently releases a dead-and-never-ran claim as unremarkable.
        reaped += _run_hook("detect_zero_turn_spawns", hook_errors,
                            detect_zero_turn_spawns, cfg, now, default=[])

        pruned_logs = 0
        pruned_bytes = 0
        errors = {}
        prune_result = _run_hook("prune_tick", hook_errors, prune_logs_tick, cfg, now, default=None)
        if prune_result:
            pruned_logs = prune_result.get("pruned_logs", 0)
            pruned_bytes = prune_result.get("pruned_bytes", 0)
            per_key_errors = prune_result.get("errors") or {}
            if per_key_errors:
                errors["prune"] = "; ".join(f"{k}: {v}" for k, v in per_key_errors.items())

    # T-52: hold the spawn-region lock from the active-read through the
    # last claim commit below -- see _spawn_lock_target's docstring for why.
    with store.file_lock(_spawn_lock_target(home)):
        active = sessions.list_active()
        # RB-14: a dispatcher-owned test-run subprocess is NOT an agent
        # session -- it must never eat into `max_concurrency`'s fleet-wide
        # agent-slot budget (that's what `TEST_RUN_CONCURRENCY` bounds
        # instead). `sessions.list_active()` for every REAL backend delegates
        # to `claims.active_keys`, which has no notion of `kind` -- filter its
        # test-run-claimed keys back out here, at the one place that budget is
        # computed, rather than teaching every backend's `list_active()` (or
        # `claims.active_keys` itself, whose other callers -- e.g. spend.py's
        # cost-settlement check -- have no reason to make the same exclusion).
        active -= {k for k, c in claims.all_claims(home).items() if c.get("kind") == "testrun"}
        due: list[tuple[str, str]] = []
        claimed: list[str] = []
        observed_seq_by_key: dict[str, int] = {}
        phase_by_key: dict[str, str] = {}
        decisions: dict[str, dict] = {}

        # MTO-4: the candidate set, restricted to `filter_keys` when given -- every
        # downstream step (due-check, throttle, claims, spawn ledger) only ever
        # sees these keys, so an unrestricted key's due-ness never even gets
        # computed, let alone spawned.
        candidates = sorted(filter_keys, key=split_key) if filter_keys is not None else list_keys(home)
        for key in candidates:
            # RB-17: never fold or spawn a key that was never minted -- no
            # spec.md, ticket directory, or event-log history. A stray
            # `derived/snapshots/<key>.json` (left by a fold that ran before
            # this guard existed, or by any other bug) is exactly what would
            # keep `list_keys` returning this key forever -- the self-
            # bootstrapping this ticket closes -- so a real sweep prunes it
            # here (never in `dry_run`, per GA-4's strictly-read-only
            # contract). A key with real event-log history but no spec.md is
            # NOT `_never_minted` -- it keeps sweeping normally below,
            # unaffected, so `maestro cmd <KEY> discard` can still retire it
            # end to end (RB-17 AC4).
            if _never_minted(home, key):
                if not dry_run:
                    snap_path = store.snapshot_path(home, key)
                    if snap_path.exists():
                        snap_path.unlink()
                decisions[key] = {
                    "outcome": "phantom",
                    "reason": "never minted -- no spec.md, ticket directory, or event log",
                }
                continue
            # RB-2: `snapshot.fold` is total by construction (never raises), but this
            # is defense-in-depth against a future/unforeseen corruption -- ONE bad
            # ticket must never abort the sweep for every other ticket on the board.
            # Same pattern as `_run_hook`, keyed per-ticket instead of per-hook-name.
            snap = _run_hook(f"fold:{key}", hook_errors, _load_and_refresh_snapshot, home, key)
            if snap is None:
                decisions[key] = {"outcome": "fold_error",
                                   "reason": hook_errors.get(f"fold:{key}", "fold failed")}
                continue
            observed_seq_by_key[key] = snap.observed_seq
            phase_by_key[key] = snap.phase
            blocked_dep = _has_unmet_deps(home, key)
            res = is_due(
                home, key, snap,
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

        # MTO-7: order the due set by (priority, key) -- a preference over WHICH due
        # key is considered first, upstream of every existing brake (spawn floor,
        # repo-blocked, max_spawns_per_sweep, max_concurrency slots, the runaway
        # brake/rate-limit/spend-ceiling gates below). It decides nothing about
        # whether a key is due, throttled, or spawnable -- only their relative order
        # through those unchanged filters, so a throttled/capped/blocked high-priority
        # ticket still can't starve the rest of the board. `spec_priority` reads fresh
        # from disk per sweep (never the folded snapshot), total and never-raises;
        # `split_key` is the tiebreaker so ordering stays deterministic when
        # priorities are equal (including the
        # all-default-3 case, which reduces to today's plain lexical order).
        due.sort(key=lambda kr: (spec_priority(home, kr[0]), split_key(kr[0])))
        bindings_by_key = {key: repos_mod.resolve(cfg, home, key) for key, _ in due}

        # T-63 (MTO-9 defect 4b): rotate the same-priority tiebreak, scoped per
        # repo -- a preference over WHICH same-priority due key comes first,
        # upstream of every gate below (repo-blocked, max_spawns_per_sweep,
        # slots), exactly like MTO-7's own priority sort is. Only reorders
        # within a (repo, priority) group; never changes group membership or
        # the across-priority order, so it can't punch through any brake
        # MTO-7's sort couldn't. See `_apply_rotation`'s docstring for the
        # degrade-safely contract on a missing/corrupt cursor file.
        rotation_cursor = store.read_json(_rotation_cursor_path(home), {})
        if not isinstance(rotation_cursor, dict):
            rotation_cursor = {}
        due = _apply_rotation(home, due, bindings_by_key, rotation_cursor)

        # Per-repo preflight, scoped to the repos this sweep's due tickets (plus the
        # implicit default) actually reference -- a 1-repo board still runs exactly
        # one probe. `repo_blockers` is the flat union, kept for existing readers
        # (heartbeat/_nudge/cmd_dispatch); `repo_blockers_by_repo` is the new
        # per-repo mapping the spawn gate below keys off of.
        preflight = repo_preflight_all(cfg, home, [k for k, _ in due])
        repo_blockers_by_repo = preflight["blockers_by_repo"]
        repo_blockers = preflight["blockers"]

        # Fleet-wide rate-limit gate. Above the human-signal bypass below: an inbox
        # answer must not punch through a 429, since that spawn would be rejected too.
        # While paused, nothing spawns and the spawn ledger is left untouched.
        paused_until_ts = ratelimit.paused_until(home, now)
        # GA-5: the runaway auto-brake, beside the rate-limit gate above. Only
        # evaluated when the rate-limit gate didn't already decide this sweep spawns
        # nothing, and never during dry_run (a strictly read-only preview must not
        # arm a pause). Arming it also makes THIS sweep spawn nothing -- mirroring
        # the rate-limit gate exactly -- while the next sweep short-circuits at G1.
        runaway_armed = (None if paused_until_ts is not None or dry_run
                         else _maybe_trip_runaway_brake(cfg, home, now, health))
        # GA-11: the daily spend ceiling, a third reason to take the same
        # spawn-nothing branch. spend.over_ceiling is a pure read, never folds --
        # so it still applies even under dry_run, off whatever spend.probe last
        # persisted (the ratelimit gate above is read the same way).
        spend_ceiling_reason = (None if paused_until_ts is not None or runaway_armed is not None
                                else spend.over_ceiling(cfg, now))
        # RB-12: the detection-channel alarm, off the exact signals just computed above --
        # never recomputed, so it can't drift from what actually gated this sweep. A write
        # (persists derived/.alarm.json, may fire notify_command/webhooks), so it's skipped
        # under dry_run like every other hook here, and never reached at all while a
        # fleet-wide pause already short-circuited the whole sweep at G1 (top of dispatch()).
        if not dry_run:
            _run_hook("alarm", hook_errors, alarm.check, cfg, now, health,
                       runaway_reason=runaway_armed, spend_ceiling_reason=spend_ceiling_reason)
        if paused_until_ts is not None or runaway_armed is not None or spend_ceiling_reason is not None:
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

            # Per-key repo-blocked gate. NOT bypassable by an _UNTHROTTLED_REASONS
            # human signal — a human answering a question is exactly the moment you
            # must not launch an agent into a half-merged tree. Skipped keys stay
            # "due" (report.due is never filtered) so the very next sweep retries
            # them once their repo is healthy again.
            repo_gated: list[tuple[str, str]] = []
            for key, reason in eligible:
                binding = bindings_by_key.get(key)
                if binding is not None and binding.name in repo_blockers_by_repo:
                    decisions[key]["outcome"] = "repo_blocked"
                    continue
                repo_gated.append((key, reason))
            eligible = repo_gated

            # Per-repo max_spawns_per_sweep safety rail (MR-5): bounds one repo's
            # blast radius on the sweep's spawn budget without touching the per-key
            # min_spawn_interval floor, the max_spawn_attempts watchdog, or this
            # fleet-wide rate gate -- all three still apply on top. None (default)
            # is uncapped, i.e. today's behavior by construction. T-63 (MTO-9
            # defect 4a): the cap must count ACTUAL launches, not intent, so
            # `per_repo_spawn_count` is no longer incremented here -- it lives
            # beside the real spawn attempt below (dry_run's would_spawn / the
            # real branch's sessions.spawn), incremented only once a key clears
            # every downstream gate. A key a later gate rejects (credential,
            # runner, interlock, attempts-exhausted) never burns its repo's
            # slot, so it can no longer starve a key behind it that would have
            # spawned cleanly.
            per_repo_spawn_count: dict[str, int] = {}

            slots = max(0, cfg.max_concurrency - len(active))
            to_spawn = eligible[:slots]
            capacity_skipped = [k for k, _ in eligible[slots:]]
            for key in capacity_skipped:
                decisions[key]["outcome"] = "capacity_skipped"

            # T-34/RF-5: the interlock -- config-only, computed once per sweep, not
            # per-key, and read here (before the dry_run split) so the read-only
            # preview reports the same refusal a real sweep would act on -- a
            # dry_run that still showed "would_spawn" for an interlocked backend
            # would make `make dry` lie about the one thing this ticket exists to
            # guarantee. While it returns a reason, no key spawns into this
            # backend at all until a bypass-resistant guard exists and is added
            # to gates.BYPASS_RESISTANT_IMPLEMENTERS (see that function's
            # docstring).
            interlock_reason = backend_interlock_reason(cfg)

            if dry_run:
                # No _allow_spawn (it can call ops.fail -> Failed/RequeueScheduled
                # events, exactly the bug this ticket exists to stop), no real
                # sessions.spawn(), no ledger/attempts write. Every to_spawn key is
                # reported as would-spawn (or would-ask, if interlocked); the next
                # REAL sweep decides for itself.
                spawned = []
                for key, _reason in to_spawn:
                    binding = bindings_by_key.get(key)
                    cap = binding.max_spawns_per_sweep if binding is not None else None
                    if cap is not None:
                        name = binding.name
                        if per_repo_spawn_count.get(name, 0) >= cap:
                            decisions[key]["outcome"] = "repo_capped"
                            continue
                    if interlock_reason is not None:
                        decisions[key]["outcome"] = "would_ask_backend_interlocked"
                        continue
                    spawned.append(key)
                    decisions[key]["outcome"] = "would_spawn"
                    if cap is not None:
                        per_repo_spawn_count[binding.name] = per_repo_spawn_count.get(binding.name, 0) + 1
            else:
                attempts_path = _spawn_attempts_path(home)
                attempts = store.read_json(attempts_path, {}) or {}
                attempts_changed = False
                # GA-17: memoized per (gh_account, token_env) for this whole sweep -- N
                # keys bound to the same repo resolve the credential once, not once each.
                credential_cache: dict = {}
                # OC-2: memoized per runner name for this whole sweep -- N keys bound
                # to the same non-claude runner probe its binary/daemon once, not once
                # each ("Probe once per sweep, cached — never per key").
                runner_probe_cache: dict = {}
                # OC-4: {runner name: count}, seeded lazily (once per runner name, on
                # first key that reaches the cap check below) from keys already in
                # `active` this sweep, then incremented in-place as this sweep spawns
                # more of that runner -- so two due opencode keys in the SAME sweep
                # can't both slip through a cap computed from a snapshot taken before
                # either spawned.
                runner_active_counts: dict[str, int] = {}
                # T-54: the seed sum below counts from the runner recorded ON EACH
                # ACTIVE KEY'S CLAIM at spawn time (`claims.write_claim`'s own
                # `runner` field), never from re-resolving `resolve_runner(cfg, k,
                # phase_by_key.get(k, ""))` -- a session spawned while `k` was
                # `implementing` still holds its slot after `k` folds to a phase
                # where its runner is no longer eligible (e.g. `qa`, by default),
                # and re-resolving would silently stop counting it, leaking a cap
                # slot (spec Notes: "the claim is the fact; count from the claim,
                # not from a re-resolution"). Loaded lazily, once for the whole
                # sweep, only if a cap check is actually reached.
                claims_by_key: dict[str, dict] | None = None

                spawned = []
                rotation_changed = False
                for key, _reason in to_spawn:
                    # RB-11: the per-key burn cap -- checked before ANY other
                    # per-key gate below (repo cap, credential, runner
                    # preflight, the no-progress attempts ledger), so a
                    # burning key is parked without spending a repo-cap slot
                    # or an attempts-ledger attempt on a spawn that would
                    # never have been let through anyway. Dead-letters (not a
                    # backoff retry) -- waiting out `max_failures` more
                    # backed-off attempts first would just reproduce the
                    # burn this exists to stop. Every OTHER due key in this
                    # sweep is untouched -- `continue` only skips this one.
                    from . import burn
                    burn_reason = burn.should_park(cfg, key)
                    if burn_reason:
                        from . import ops
                        ops.fail(cfg, key, burn_reason, actor="dispatcher",
                                 dead_letter=True, kind="burn")
                        reaped.append(key)
                        decisions[key]["outcome"] = "burn_parked"
                        continue
                    binding = bindings_by_key.get(key)
                    cap = binding.max_spawns_per_sweep if binding is not None else None
                    if cap is not None and per_repo_spawn_count.get(binding.name, 0) >= cap:
                        decisions[key]["outcome"] = "repo_capped"
                        continue
                    if interlock_reason is not None:
                        # Ask (not fail): this is a config/backend gap, not a per-key
                        # error, and asking parks the key in awaiting-human -- visibly,
                        # once (idempotent qid) -- instead of respawning it into the same
                        # refusal every sweep. No attempts-ledger spend either, same as
                        # the credential_unresolvable branch below.
                        decisions[key]["outcome"] = "backend_interlocked"
                        from . import ops
                        ops.ask(cfg, key, interlock_reason, qid=f"backend-interlock-{key}",
                                actor="dispatcher")
                        continue
                    cred = resolve_credential(binding, credential_cache)
                    if not cred.ok:
                        # Fail closed (GA-17): never spawn into a repo whose configured
                        # credential can't be resolved, and never fall back to the
                        # ambient `gh` account -- that's precisely the silent-404 mode
                        # this ticket exists to remove. No attempts-ledger spend either;
                        # this is a config problem, not a no-progress spawn.
                        decisions[key]["outcome"] = "credential_unresolvable"
                        from . import ops
                        ops.fail(cfg, key,
                                f"gh credential unresolvable for repo "
                                f"'{binding.name if binding is not None else 'default'}': {cred.error}",
                                actor="dispatcher")
                        continue
                    runner, runner_model = resolve_runner(cfg, key, phase_by_key.get(key, ""))
                    if runner not in _REGISTERED_RUNNERS:
                        # RF-2: fail closed (never fall back to Claude, same rule GA-17
                        # states verbatim for credentials) -- ask, don't fail, and spend
                        # no attempts-ledger slot: this is a config problem for a human
                        # to fix (register the runner, or fix the spec's `runner:` line),
                        # not a no-progress spawn attempt.
                        decisions[key]["outcome"] = "runner_unregistered"
                        from . import ops
                        ops.ask(cfg, key,
                                f"spec names runner {runner!r}, which has no registered "
                                f"SessionManager (registered: {sorted(_REGISTERED_RUNNERS)}) -- "
                                "fix the spec's `runner:` line or register the runner",
                                qid=f"unregistered-runner-{key}-{runner}", actor="dispatcher")
                        continue
                    if runner != "claude":
                        # OC-2: fail-closed preflight for a registered non-claude
                        # runner -- a fast-failing runner is the worst failure mode
                        # here (a claim file is the ONLY other per-key spawn memory
                        # and is unlinked the moment the worker dies, so a runner
                        # that exits in under a second leaves nothing behind and the
                        # key is instantly re-spawnable, the 21,731-spawn/~$845
                        # shape). Binary missing/unexecutable or the ollama daemon
                        # unreachable are TRANSIENT: a group-level skip modelled on
                        # `repo_blocked` above -- no event, no attempts-ledger spend,
                        # so a 30s daemon outage doesn't walk the board into
                        # `degraded` one ticket at a time (AC1/AC2/AC5). Model
                        # absent or not tool-capable is PERMANENT: `ops.ask`, same
                        # shape as `runner_unregistered` just above -- a human must
                        # fix the spec, so no attempts-ledger spend either (AC3/AC4).
                        # Never falls through to `sessions.spawn` on any branch, so
                        # a bad runner can never silently spawn under claude (AC6).
                        # OC-3: the board-wide kill switch -- consulted before ANY of
                        # OC-2's own preflight (before the binary probe, before the
                        # daemon probe). A disabled runner is a config problem, not a
                        # transient one, so it gets the same PERMANENT `ops.ask`
                        # treatment as `runner_unregistered`/`runner_model_unavailable`
                        # above: no attempts-ledger spend, a stable qid, parked in
                        # awaiting-human for a human to flip `runner_enabled` or fix
                        # the spec's `runner:` line.
                        if runner not in cfg.runner_enabled:
                            decisions[key]["outcome"] = "runner_disabled"
                            from . import ops
                            ops.ask(cfg, key,
                                    f"runner {runner!r} is not enabled on this board "
                                    f"(runner_enabled={sorted(cfg.runner_enabled)}) -- flip "
                                    "the board config's `runner_enabled` or fix the spec's "
                                    "`runner:` line",
                                    qid=f"runner-disabled-{key}-{runner}", actor="dispatcher")
                            continue
                        probe_fn = runner_probe or _make_default_runner_probe(cfg)
                        if runner not in runner_probe_cache:
                            runner_probe_cache[runner] = probe_fn(runner)
                        probed = runner_probe_cache[runner]
                        if not probed.get("binary_ok"):
                            decisions[key]["outcome"] = "runner_binary_missing"
                            runner_blockers[runner] = f"{runner!r} binary not found on PATH"
                            continue
                        if probed.get("models") is None:
                            decisions[key]["outcome"] = "runner_daemon_unreachable"
                            runner_blockers[runner] = (f"{runner!r} daemon unreachable: "
                                                        f"{probed.get('daemon_reason')}")
                            continue
                        if not runner_model:
                            verdict, vreason = "missing", "no runner_model configured"
                        else:
                            verdict_resolver = runner_verdict or _make_default_runner_verdict(cfg)
                            verdict_fn = verdict_resolver(runner)
                            verdict, vreason = verdict_fn(
                                probed["models"], probed.get("daemon_reason"), runner_model)
                        if verdict != "ok":
                            decisions[key]["outcome"] = "runner_model_unavailable"
                            from . import ops
                            ops.ask(cfg, key,
                                    f"runner {runner!r} model {runner_model!r} unavailable: "
                                    f"{vreason} -- fix the spec's `runner_model:` line or "
                                    "install/pull the model",
                                    qid=f"runner-model-{key}-{runner}-{runner_model}",
                                    actor="dispatcher")
                            continue
                        # OC-4: per-runner concurrency cap -- still OC-2's own preflight
                        # (before `_allow_spawn`, no attempts-ledger spend), because the
                        # thing bounding a fast-failing runner's spawn RATE must never be
                        # the no-progress circuit breaker (that would burn `max_failures`
                        # attempts and dead-letter the ticket over what is, here, pure
                        # capacity -- not a broken ticket). Group-level skip, modelled on
                        # `repo_capped`/`repo_blocked`: no event, ticket stays due, retried
                        # next sweep once a slot frees up.
                        cap = _runner_concurrency_cap(cfg, runner)
                        if cap is not None:
                            if runner not in runner_active_counts:
                                if claims_by_key is None:
                                    claims_by_key = claims.all_claims(home)
                                runner_active_counts[runner] = sum(
                                    1 for k in active
                                    if (claims_by_key.get(k) or {}).get("runner", "claude") == runner)
                            if runner_active_counts[runner] >= cap:
                                decisions[key]["outcome"] = "runner_capped"
                                continue
                            runner_active_counts[runner] += 1
                    if not _allow_spawn(cfg, key, observed_seq_by_key.get(key, 0), attempts):
                        attempts_changed = True
                        reaped.append(key)
                        decisions[key]["outcome"] = "attempts_exhausted"
                        continue
                    attempts_changed = True
                    cwd = _worker_cwd(cfg, key)
                    command = resolve_reconcile_command(cfg, phase_by_key.get(key, ""))
                    model, effort = _resolve_model_effort(cfg, key)
                    disallowed_tools = MERGE_DENYLIST + phase_denylist(phase_by_key.get(key, ""))
                    allowed_tools = resolved_allowed_tools(cfg, binding)
                    # RF-1: hand the resolved command and key to spawn() as separate
                    # arguments -- each backend composes its own invocation (the Claude
                    # CLI concatenates "<command> <key>" into one prompt string; a
                    # non-Claude backend, e.g. opencode, can take them as distinct
                    # `--command <name>` + argument instead of string-parsing a slash
                    # command back out of a prompt maestro pre-flattened).
                    sessions.spawn(key, command, cwd, model=model, effort=effort,
                                   disallowed_tools=disallowed_tools, allowed_tools=allowed_tools,
                                   env_overlay=cred.env, runner=runner, runner_model=runner_model)
                    spawned.append(key)
                    decisions[key]["outcome"] = "spawned"
                    if cap is not None:
                        per_repo_spawn_count[binding.name] = per_repo_spawn_count.get(binding.name, 0) + 1
                    # T-63 (MTO-9 defect 4b): remember this key as the last one
                    # actually spawned for its (repo, priority) group, so the
                    # NEXT sweep's `_apply_rotation` starts that group just
                    # past it -- true round-robin, not favoring the same key
                    # every sweep. Only an actual launch advances the cursor
                    # (never a dry-run "would_spawn"), matching 4a's own
                    # actual-vs-intent rule.
                    repo_name = binding.name if binding is not None else "default"
                    priority = spec_priority(home, key)
                    rotation_cursor.setdefault(repo_name, {})[str(priority)] = key
                    rotation_changed = True
                    weight = spawn_weight(cfg, phase_by_key.get(key, ""))
                    prev = ledger.get(key)
                    recent = list(prev.get("recent", [])) if isinstance(prev, dict) else []
                    recent.append([now, weight])
                    recent = [e for e in recent
                              if (ts := _ledger_entry_ts(e)) is not None
                              and now - ts <= health.WINDOW_SECONDS][-_LEDGER_RECENT_CAP:]
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
                if rotation_changed:
                    store.write_json(_rotation_cursor_path(home), rotation_cursor)

    # Heartbeat/decision-ledger writes are dashboards, not the source of truth --
    # both regenerate from state, so they're written either way. Under dry_run,
    # `spawned` holds would-spawn keys for the REPORT (report.spawned is the
    # whole point of a preview) but the heartbeat's "spawned" count -- read by
    # fleet-health/runaway detection as "N sessions actually launched this
    # sweep" -- must stay 0, not the phantom would-spawn count; likewise
    # `decisions[key]["outcome"]` is "would_spawn", never "spawned", so
    # `maestro why` can't report a spawn that never happened either.
    _write_heartbeat(home, now, 0 if dry_run else len(spawned), len(active),
                     len(throttled), len(due),
                     repo_blockers=repo_blockers, repo_blockers_by_repo=repo_blockers_by_repo)
    _append_dispatch_ledger(home, {
        "ts": store.iso_now(), "epoch": now,
        "hook_errors": hook_errors, "decisions": decisions,
    })
    return DispatchReport(
        minted=minted, due=due, claimed=claimed, spawned=spawned,
        capacity_skipped=capacity_skipped, active_sessions=len(active),
        scheduled_fired=scheduled_fired, throttled=throttled,
        pruned_logs=pruned_logs, pruned_bytes=pruned_bytes, errors=errors,
        paused_until=paused_until_ts, spend_ceiling_reason=spend_ceiling_reason, reaped=reaped,
        repo_blockers=repo_blockers, repo_blockers_by_repo=repo_blockers_by_repo,
        runner_blockers=runner_blockers,
        hook_errors=hook_errors,
        worktree_removal_errors=worktree_removal_errors,
        would_mint=would_mint,
    )


# T-22: progressive disclosure -- one reconcile skill per phase, instead of a
# single file every reconciler loaded in full regardless of what its phase
# needed. Phases whose reconciler step is dispatcher-owned or a no-op share
# the "passive" file; an unrecognized phase also lands there rather than on
# a nonexistent command.
_PHASE_COMMAND_SUFFIX = {
    Phase.TRIAGING: "triaging",
    Phase.AWAITING_HUMAN: "awaiting-human",
    Phase.READY: "ready",
    Phase.RESEARCHING: "researching",
    Phase.IMPLEMENTING: "implementing",
    Phase.QA: "qa",
    Phase.AWAITING_CI: "passive",
    Phase.IN_REVIEW: "passive",
    # RB-14: sleeping, dispatcher-owned (`sync_test_runs`) -- never actually
    # spawned, since `is_due` has no signal that wakes `verifying`, but listed
    # here anyway for the same documentation-completeness reason AWAITING_CI/
    # IN_REVIEW are: if it ever somehow WERE spawned, "passive" is the
    # correct, harmless fallback (a no-op reconcile), never a nonexistent
    # `/maestro-reconcile-verifying` command.
    Phase.VERIFYING: "passive",
    Phase.DEGRADED: "passive",
    Phase.TERMINATING: "passive",
}


def resolve_reconcile_command(cfg: Config, phase: str) -> str:
    """The ``claude -p`` command to spawn for *phase* (T-22): resolved once,
    here, beside ``_resolve_model_effort``/``MERGE_DENYLIST`` -- the dispatcher
    already resolves per-key spawn state at this point, so per-phase command
    routing is the same shape, not a new mechanism. ``maestro env --key`` also
    surfaces this (see ``cli.cmd_env``) so ``make reconcile`` routes identically
    to a real dispatcher spawn instead of re-deriving the mapping in Make/bash.
    """
    try:
        suffix = _PHASE_COMMAND_SUFFIX[Phase(phase)]
    except (ValueError, KeyError):
        suffix = "passive"
    return f"{cfg.reconcile_command}-{suffix}"


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


# RF-2/OC-4/PI-8: the registered runners -- the seam RF-2 landed (spec
# `runner:`/`runner_model:` -> resolve_runner -> RoutingSessions) was proved
# byte-identical-by-default before a second backend was added, per RF-2's ticket
# Intent. OC-4 added the second backend (``sessions.OpencodeCliSessions``);
# PI-8 the third (``sessions.PiCliSessions``). Adding a FOURTH means adding it
# here AND wiring a delegate into every RoutingSessions construction site
# (cli.py) -- one is meaningless without the other (T-53's parity test,
# tests/test_runner_registry_parity.py, fails loudly if either is missed).
_REGISTERED_RUNNERS = frozenset({"claude", "opencode", "pi"})

# OC-4/PI-8: per-runner concurrency cap defaults, consulted by
# `_runner_concurrency_cap` below when `[runner.<name>]`'s `concurrency` key
# (cfg.provider_config) is unset. "claude" is never looked up here -- the
# fleet-wide `max_concurrency` already bounds it, and it never reaches the
# non-claude branch this cap gates. opencode defaults to 1 (spec Notes, QW-3):
# it wedges intermittently at `init` and every process shares one on-disk
# sqlite db (`~/.local/share/opencode/opencode.db` + WAL), so two concurrent
# opencode reconcilers is a measured risk until proven otherwise. pi ALSO
# defaults to 1 (PI-8 spec Notes) -- not a performance default, an account
# fair-use one: the likely provider's subscription is documented as scoped to
# 1-2 concurrent projects, with enforcement against exactly maestro's fan-out
# pattern -- raise either via config, never by editing the default.
_RUNNER_DEFAULT_CONCURRENCY = {"opencode": 1, "pi": 1}

# T-54/PI-8: per-runner eligible-phase defaults, consulted by
# `runner_eligible_phases` below when `[runner.<name>]`'s `phases` key
# (cfg.provider_config) is unset -- "implementing only" for a runner with NO
# entry here at all (see `runner_eligible_phases`'s own fallback), matching
# what `resolve_runner` hardcoded before T-54, so an existing home with no
# `[runner.<name>] phases` line spawns byte-identically to before. pi gets an
# EXPLICIT empty entry here -- deliberately narrower than that universal
# fallback: PI-8's spec Notes are explicit that admitting pi to `implementing`
# is a separate, reviewable config change made only after the guard has been
# proven in production, not something this ticket's own registration should
# grant for free via the generic default. Admitting any runner to a phase
# beyond its default is always a deliberate config edit
# (`[runner.<name>] phases = [...]`), never a code change.
_RUNNER_DEFAULT_PHASES = {"opencode": frozenset({Phase.IMPLEMENTING}), "pi": frozenset()}


def runner_eligible_phases(cfg: Config, runner: str) -> frozenset:
    """The set of `Phase`s *runner* may actually be spawned for (T-54).

    Sourced from `[runner.<name>].phases` (``cfg.provider_config["runner"]
    [name]["phases"]``, a list of phase-suffix strings, e.g. ``["implementing",
    "qa"]``) if set, else `_RUNNER_DEFAULT_PHASES.get(runner)` -- which itself
    falls back to ``{Phase.IMPLEMENTING}`` when *runner* has no default entry
    either, so "implementing only" is the universal default. Same fail-soft
    idiom as `resolve_reconcile_command`'s own `try/except ValueError` around
    `Phase(phase)`: an unparseable phase name in the config list is skipped,
    never raised -- a config typo here must never wedge the spawn loop, only
    narrow the eligible set by one entry. Never raises, never spawns a
    subprocess -- called from `resolve_runner`, which must stay a pure
    config + spec read (see its own docstring).
    """
    table = (cfg.provider_config.get("runner") or {}).get(runner) or {}
    raw = table.get("phases")
    if raw is None:
        return _RUNNER_DEFAULT_PHASES.get(runner, frozenset({Phase.IMPLEMENTING}))
    out = set()
    for name in raw if isinstance(raw, list) else []:
        try:
            out.add(Phase(name))
        except ValueError:
            continue
    return frozenset(out)


def _runner_concurrency_cap(cfg: Config, runner: str) -> int | None:
    """The per-runner concurrency cap (OC-4) for *runner* -- `[runner.<name>]`'s
    `concurrency` key (``cfg.provider_config["runner"][name]["concurrency"]``,
    validated at ``config.load`` against the fail-closed key allowlist there) if
    set, else ``_RUNNER_DEFAULT_CONCURRENCY.get(runner)`` (None = uncapped, e.g.
    every runner besides opencode until one is configured). Never raises -- a
    malformed value (not int-able) falls back to the default rather than wedging
    the spawn loop over a config typo `config.load` didn't already reject."""
    table = (cfg.provider_config.get("runner") or {}).get(runner) or {}
    raw = table.get("concurrency")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _RUNNER_DEFAULT_CONCURRENCY.get(runner)


def resolve_runner(cfg: Config, key: str, phase: str) -> tuple[str, str | None]:
    """Resolve the runner (and its optional model override) for spawning *key*'s
    reconciler (RF-2), for *phase* (T-54).

    Requirement 1, enforced HERE and only here ("THE RULE MUST HAVE EXACTLY ONE
    DEFINITION", the same posture ``gates.runner_editable`` documents): the
    spec's `runner:` choice only
    ever takes effect for a phase admitted into that runner's own eligible-phase
    set (`runner_eligible_phases`, a `Phase`-keyed table sourced from
    `[runner.<name>].phases` config, same try/except-``ValueError`` shape as
    `resolve_reconcile_command`'s `_PHASE_COMMAND_SUFFIX` lookup) -- an
    unparseable *phase*, or a phase absent from that set, both fall back to
    ``("claude", None)`` regardless of what the spec says. By default that set
    is `implementing` only for every runner, so an existing home with no
    `[runner.<name>] phases` line resolves byte-identically to before this
    ticket; admitting a runner to `triaging`/`researching`/`qa` too is a
    deliberate config change, not a code edit.

    Precedence when the phase IS eligible: spec `runner:`/`runner_model:`
    front-matter -> board config (``cfg.runner``/``cfg.runner_model``) -- two
    levels only, matching `model`/`effort` (no ``[repos.*].runner`` layer).

    Pure config + spec reads -- never spawns a subprocess (``maestro env --key``
    is a hot path every reconciler preamble calls; see
    ``tests/test_gh_credentials.py`` for the no-subprocess assertion this must
    keep passing) and never raises: a missing spec file, an empty spec, or a
    malformed `runner:` line all fall back to the config default rather than
    wedging spawn-arg construction. Returns the runner name VERBATIM, whatever
    it is -- validating it's actually registered (``_REGISTERED_RUNNERS``) and
    failing closed on an unregistered name is the caller's job (``dispatch``),
    not this function's, so this stays a pure read with no side effects.
    """
    try:
        phase_enum = Phase(phase)
    except ValueError:
        return "claude", None

    spec_file = store.spec_path(cfg.home, key)
    overrides: dict = {}
    if spec_file.exists():
        overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))

    runner = overrides.get("runner") or cfg.runner
    runner_model = overrides.get("runner_model") or cfg.runner_model
    if phase_enum not in runner_eligible_phases(cfg, runner):
        return "claude", None
    return runner, runner_model


def _default_runner_probe(runner: str) -> dict:
    """OC-2's real preflight probe for a non-``claude`` *runner*: is its own CLI
    binary on ``PATH`` (``shutil.which`` -- covers "missing" and "unexecutable"
    in one check, since ``which`` already applies ``os.X_OK``), and, separately,
    is the local ollama daemon reachable with its model catalogue
    (``providers.ollama.fetch_models``, used below to verify a specific
    ``runner_model`` is installed and tool-capable). Never raises -- like
    ``fetch_models`` itself and ``resolve_credential`` beside it, a down binary
    or daemon must never wedge the spawn loop, only gate the one key that needs
    it. Never called for ``runner == "claude"`` (see the spawn loop below), and
    the caller -- never this function -- is what caches one call per sweep per
    runner name instead of once per key.
    """
    from .providers import ollama as ollama_mod

    models, daemon_reason = ollama_mod.fetch_models()
    return {"binary_ok": shutil.which(runner) is not None,
            "models": models, "daemon_reason": daemon_reason}

def _make_default_runner_probe(cfg: Config) -> Callable[[str], dict]:
    """Creates a closure that builds the default runner probe function.
    
    The returned closure will dispatch to different probes based on runner name.
    For unrecognized runners it falls back to the ollama probe to maintain 
    backward compatibility.
    
    This is called from within dispatch(), and the result is used as the 
    default probe_fn when runner_probe=None, to support both:

    a) Tests that inject one-arg probes (lambda runner: {...})
    b) The new per-runner dispatch table approach
    """
    # Build a dispatch table for different runners
    def _probe(runner: str) -> dict:
        # Create probe functions for different runner types
        def _ollama_probe() -> dict:
            from .providers import ollama as ollama_mod
            models, daemon_reason = ollama_mod.fetch_models()
            return {"binary_ok": shutil.which("ollama") is not None,
                    "models": models, "daemon_reason": daemon_reason}

        def _pi_probe() -> dict:
            # T-57 (PI-5): run under maestro's own PI_CODING_AGENT_DIR (PI-4)
            # so this never depends on -- or pollutes -- a developer's real
            # `~/.pi`; `providers.pi.fetch_models` itself sets pi's offline
            # flag. `models is None` (fetch_models' failure AND zero-models
            # shape, see its own docstring) is exactly the generic
            # `probed.get("models") is None` branch below, so a credential-
            # less/unreachable pi is TRANSIENT with no special-casing here.
            from . import store as store_mod
            from .providers import pi as pi_mod
            models, reason = pi_mod.fetch_models(store_mod.pi_agent_dir(cfg.home))
            return {"binary_ok": shutil.which("pi") is not None,
                    "models": models, "daemon_reason": reason}

        def _generic_probe() -> dict:
            # For runners like opencode that don't have special support yet
            # we use the existing ollama probe for compatibility, but this would
            # be the place to add true runner-specific probing in future
            return _default_runner_probe(runner)

        # Dispatch table for different runner types
        probe_dispatch = {
            "ollama": _ollama_probe,
            "pi": _pi_probe,
            # Other runners can have their own specific probes later
        }
        
        # Get the appropriate probe function, fallback to generic
        probe_fn = probe_dispatch.get(runner, _generic_probe)
        return probe_fn()

    return _probe


def _make_default_runner_verdict(cfg: Config) -> Callable[[str], Callable]:
    """The default resolver for the permanent model-verdict branch: given a
    runner name, returns the ``(models, daemon_reason, runner_model) ->
    (verdict, reason)`` function that judges whether that runner's catalogue
    has ``runner_model`` installed and tool-capable. Mirrors
    ``_make_default_runner_probe``'s ``{runner name: fn}`` dispatch table so
    the two preflight checks generalize the same way -- a genuinely
    unrecognized runner name (no dedicated verdict logic of its own yet)
    falls back to the existing ollama-backed verdict, matching the spec's
    Note that unrecognized runners fall back to the ollama probe. Called from
    within ``dispatch()``; the result is used as the default resolver when
    ``runner_verdict=None``, so injected one-arg fakes
    (``lambda runner: fake_verdict_fn``) and this real dispatch table share
    the same call shape.
    """
    def _resolve(runner: str) -> Callable:
        def _ollama_verdict() -> Callable:
            from .providers import ollama as ollama_mod
            return ollama_mod.verdict_for_model

        def _pi_verdict() -> Callable:
            from .providers import pi as pi_mod
            return pi_mod.verdict_for_model

        # Dispatch table for different runner types -- other runners can get
        # their own dedicated verdict function here as they gain real support.
        verdict_dispatch = {
            "ollama": _ollama_verdict,
            "pi": _pi_verdict,
        }
        verdict_fn_getter = verdict_dispatch.get(runner, _ollama_verdict)
        return verdict_fn_getter()

    return _resolve


def _worker_cwd(cfg: Config, key: str) -> Path:
    """Where the reconciler runs. Prefer the ticket's own worktree. Before one
    exists (e.g. the first triage step, or ``implementing`` if its worktree was
    removed): ``mode == "local"`` bindings (AD-6 -- a plain, non-git target
    directory, the deliberate write-in-place case) still land in
    ``binding.path`` unchanged. A ``git``-mode binding never does (QW-7) --
    on a single-repo board ``binding.path`` IS the human's own shared checkout,
    and every pre-worktree phase grants ``Write``/``Edit`` with
    ``--permission-mode acceptEdits``, so a relative edit would land there
    instead of in ticket-owned state. Use a per-key scratch dir instead,
    seeded with the repo's ``.claude/commands`` so the phase's slash command
    still resolves. Only if no repo is configured at all (no path to seed
    from) do we land in ``home`` (which has no ``.claude/commands`` either --
    the reconciler would no-op there, same as before this ticket)."""
    wt = store.worktree_path(cfg.home, key)
    if wt.exists():
        return wt
    from . import repos as repos_mod  # lazy: repos imports us at module load time

    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode == "local":
        if binding.path:
            return Path(binding.path)
        return cfg.home
    if binding.path and Path(binding.path).exists():
        return _ensure_scratch_dir(cfg.home, key, Path(binding.path))
    return cfg.home


def _ensure_scratch_dir(home: Path, key: str, repo_path: Path) -> Path:
    """Idempotently create/refresh the per-key scratch cwd ``_worker_cwd`` uses
    for a ``git``-mode ticket before its worktree exists (QW-7): a plain,
    non-git directory under ``home/scratch/<key>/`` -- never *repo_path*
    itself -- with a symlink to *repo_path*'s ``.claude/commands`` (only that
    dir, not the whole repo; cwd-based command resolution is already proven by
    the fallback this replaces, and ``--add-dir``'s effect on slash-command
    discovery from a different cwd is unverified in this codebase, so it isn't
    load-bearing here) so the phase's ``/maestro-reconcile-<phase>`` command
    still resolves. Safe to call every sweep: re-links only if the target repo
    path changed, never touches an unexpected real file/dir at that path."""
    scratch = store.scratch_path(home, key)
    scratch.mkdir(parents=True, exist_ok=True)
    commands_src = repo_path / ".claude" / "commands"
    if not commands_src.exists():
        return scratch
    commands_link = scratch / ".claude" / "commands"
    commands_link.parent.mkdir(parents=True, exist_ok=True)
    if commands_link.is_symlink():
        if commands_link.resolve() != commands_src.resolve():
            commands_link.unlink()
            commands_link.symlink_to(commands_src)
    elif not commands_link.exists():
        commands_link.symlink_to(commands_src)
    # else: a real file/dir already sits there -- never clobber it.
    return scratch


def _write_heartbeat(home: Path, now: float, spawned: int, active: int,
                     throttled: int = 0, due: int = 0, *, paused: bool = False,
                     repo_blockers: list[str] | None = None,
                     repo_blockers_by_repo: dict | None = None) -> None:
    store.write_json(home / "derived" / ".heartbeat.json",
                     {"ts": store.iso_now(), "epoch": now,
                      "spawned": spawned, "active": active,
                      "throttled": throttled, "due": due, "paused": paused,
                      "repo_blockers": repo_blockers or [],
                      "repo_blockers_by_repo": repo_blockers_by_repo or {}})
