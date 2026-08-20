"""High-level reconciler operations — the verbs an agent uses, each correct by
construction. Every verb: appends event(s) with a deterministic step-id, then
refreshes the snapshot. Idempotent under crash-and-respawn -- the one exception
is ``worktree_ensure`` (GA-20), whose idempotence is a worktree-local marker
file rather than an event, by design (see its docstring).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import backup
from . import events as E
from . import context as context_mod
from . import config as config_mod
from . import gates
from . import event_log, inbox, repos as repos_mod, schedule as schedule_mod, snapshot as snap_mod, store
from . import testlang
from .config import Config
from .dispatcher import spec_hash_on_disk
from .idempotency import content_hash, step_id
from .statemachine import Phase, can_transition

ANSWER_COMMANDS = {"ans", "answer", "approve", "yes", "ok", "no", "reject", "discard", "retry"}


def _refuse_unminted(cfg: Config, key: str) -> None:
    """RB-17: refuse to append the FIRST event ever recorded for *key*.

    A reconciler mistakenly spawned for a key that was never minted via
    `maestro create` (a typo'd `--key`, a stale `make reconcile KEY=...`, ...)
    finds no spec.md and no ticket directory, and its own discovery of that --
    typically an eventual `maestro fail "no spec.md found"` -- used to be the
    FIRST event ever appended for that key. That single append is what made
    the key real: `dispatcher.list_keys` unions ticket dirs, event logs AND
    snapshots, so the moment an event log exists the key is permanently
    swept, and every respawn just re-discovers the same absence and fails
    again (the T-999 incident this ticket's spec documents -- four `Failed`
    events and a dead-letter entry for a key that never should have existed).
    Checked here, the one append chokepoint every mutating verb in this
    module funnels through, rather than duplicated per-verb.

    Deliberately does NOT block a key that already has event-log history,
    however it got there -- including a legacy phantom that bootstrapped
    before this guard existed. That key is already real: `maestro cmd <KEY>
    discard` must keep working on it end to end (RB-17 AC4), and this guard's
    whole job is stopping the FIRST event, not stranding one that already has
    a history. `dispatcher.list_keys`'s archived/dead-letter exclusion is
    unaffected -- this never touches list membership, only appends.
    """
    if store.spec_path(cfg.home, key).exists():
        return
    if store.ticket_dir(cfg.home, key).exists():
        return
    if event_log.read(cfg.home, key):
        return
    raise store.MaestroError(
        f"{key}: refusing to append -- no spec.md, ticket directory, or event "
        f"log exists for this key; it was never minted via `maestro create`. "
        f"Not appending the event that would bootstrap a phantom ticket into "
        f"existence (RB-17).")


def _append(cfg: Config, key: str, type: str, payload: dict, *, actor: str,
            sid: str | None, expect: int | None = None) -> dict | None:
    _refuse_unminted(cfg, key)
    ev = event_log.append(cfg.home, key, type, payload, actor=actor,
                          step_id=sid, expected_last_seq=expect)
    snap_mod.rebuild(cfg.home, key)
    context_mod.regenerate(cfg.home, key)
    return ev


def set_phase(cfg: Config, key: str, phase: Phase, *, reason: str = "", actor: str = "reconciler",
              requeue_in: int | None = None, expect: int | None = None, force: bool = False) -> dict | None:
    """Advance the ticket's phase.

    T-80: refuses (raises `MaestroError`, NO event appended, unconditionally --
    `force` does not override this one, same posture as the QA-failing gate
    below) a handoff to `qa` or entry into `awaiting-ci` while the CURRENT
    spec.md parses to zero acceptance criteria (`snapshot.has_acs`) -- the
    fail-closed half of T-80: the dispatcher's own due-gate (`dispatcher.dispatch`)
    is what stops a zero-AC ticket from ever reaching `implementing` in the
    ordinary case, but a human can still edit ACs back out from under a
    ticket already past that point, and this is the write-path backstop for
    that. Checked against the phase the CALLER asked for, before the
    `verifying` redirect below can substitute a different one -- refusing the
    `qa` handoff itself is the point, independent of whether it would have
    been redirected.

    Entering `awaiting-ci` is gated three ways: a ticket with unattested ACs
    refuses (raises `MaestroError`, non-zero exit, NO event appended) unless
    `force=True`; one with a failing independent QA verdict on a current AC
    refuses unconditionally (`force` does not override QA — that gate is fixed
    by re-running `maestro qa-verdict`, not by a human override); and (T-85,
    `cfg.awaiting_ci_qa_gate`, default ON) one where any current AC has no
    *passing* spec-axis QA verdict at all -- not merely no failing one -- also
    refuses unconditionally, same posture as the QA-failing gate (see
    `_refuse_if_qa_incomplete`).

    Entering `qa` from `implementing` is handled a third way (RB-14, replacing
    RB-12's earlier raise-or-force gate): if a `test_command` resolves for
    this ticket (T-83: its repo's own override, or the board-wide default)
    and this ticket isn't a `mode: local` binding, the call is transparently REDIRECTED
    to `verifying` instead — never refused, and `force` has no effect on this
    one (there is nothing left to force past: `verifying` is where the
    dispatcher's own `sync_test_runs` tick runs the suite itself, a real
    subprocess, and routes on to `qa` or back to `implementing` once it has an
    answer — see `dispatcher.sync_test_runs`). This is what dissolves the old
    `--force` hole (T-71's spec Notes: a forced transition could still land a
    captured-red tree in front of QA) — there's no longer a decision here for
    `--force` to override. `_tests_stale_reason` decides whether the redirect
    is even needed: unconfigured, `mode: local`, or a CURRENT tree state that
    already has a captured PASSING record all skip straight to `qa` exactly as
    before (ships dark; see `Config.test_command`).

    The `--force` escape hatch above is for a human overriding the
    unverified-ACs gate only now; the event log still has to show that they
    did, so a forced transition records `forced_by=<actor>` on the
    PhaseChanged event plus a Note naming what was skipped.

    `expect` (RB-7) is the fencing CAS, scoped to this one call site — the
    state-machine gate — and to nowhere else in `ops`: a caller that already
    holds the folded snapshot it decided `phase` from passes `expect=<that
    snapshot's observed_seq>`, and a lost race (something else appended after
    that fold) raises `event_log.StaleAppendError` instead of silently
    committing a decision made on stale data. `None` (the default) means the
    caller isn't asserting freshness — no CAS is applied. Callers that already
    hold that fold for free (`ask`, `ask_round`, `route_conflict`,
    `route_stale`, `dispatcher._observe_ci`, `dispatcher._observe_reviews`)
    pass it; `StaleAppendError` is deliberately left to propagate uncaught —
    it means "someone else moved this ticket first," which is a benign lost
    race, not a failure: the dispatcher's level-triggered sweep re-derives the
    ticket next pass, and nothing here should spend `failure_count` on it.
    """
    snap = snap_mod.load(cfg.home, key)
    if phase in (Phase.QA, Phase.AWAITING_CI):
        _refuse_if_missing_acs(cfg, key, phase)
    unverified = _acs_unverified_count(cfg, key, snap) if phase == Phase.AWAITING_CI else 0
    if unverified > 0 and not force:
        raise store.MaestroError(
            f"{key}: refusing awaiting-ci — {unverified} acceptance criteria unverified; "
            f"run `maestro verify-ac` for each, or pass --force to override")
    forced_acs = unverified > 0 and force

    # RB-14: transparent redirect, never a refusal and never forceable -- see
    # this function's docstring. `force` is deliberately not consulted here.
    if phase == Phase.QA and Phase(snap.phase) == Phase.IMPLEMENTING:
        if _tests_stale_reason(cfg, key, snap) is not None:
            phase = Phase.VERIFYING

    if phase == Phase.AWAITING_CI:
        _refuse_if_qa_failing(cfg, key, snap)
        _refuse_if_qa_incomplete(cfg, key, snap)

    src = Phase(snap.phase)
    if src != phase and not can_transition(src, phase):
        # Not fatal — log it, but the engine trusts the agent's judgment.
        _append(cfg, key, E.NOTE, {"text": f"unusual transition {src.value}->{phase.value}"},
                actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, f"note-transition-{phase.value}"))
        snap = snap_mod.load(cfg.home, key)

    payload = {"phase": phase.value, "reason": reason}
    if forced_acs:
        payload["forced_by"] = actor
    sid = step_id(key, snap.phase, snap.observed_seq, f"phase:{phase.value}")
    ev = _append(cfg, key, E.PHASE_CHANGED, payload, actor=actor, sid=sid, expect=expect)
    if forced_acs:
        _append(cfg, key, E.NOTE,
                {"text": f"forced past {unverified} unverified acceptance criteria by {actor}"},
                actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, "force-ac-override"))
    if not forced_acs and phase == Phase.AWAITING_CI:
        _warn_unverified_acs(cfg, key, actor=actor)
    if requeue_in is not None:
        requeue(cfg, key, requeue_in, actor=actor)
    return ev


def _annotations_active(cfg: Config, key: str) -> bool:
    """T-79: whether the annotation-aware AC gate is live for *key* right now --
    exactly the same ships-dark conditions as the T-74/RB-12 suite gate itself
    (`_tests_stale_reason`): a `test_command` resolved for *key* (T-83: its
    repo's own override, or the board-wide default), and *key* not bound
    `mode: local` (no repo/suite to run there). False means every annotated AC
    falls back to plain self-attestation, byte-identical to a spec with no
    annotations at all (see `snapshot.Snapshot.acs_unverified`'s `tree_key=None`
    behavior)."""
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if not binding.test_command:
        return False
    return binding.mode != "local"


def _current_tree_key(cfg: Config, key: str) -> str:
    from .dispatcher import _worker_cwd  # lazy: avoid a module-load cycle, mirrors qa_brief

    return _tree_state_key(_worker_cwd(cfg, key))


def _refuse_if_missing_acs(cfg: Config, key: str, phase: Phase) -> None:
    """T-80: raise (no event appended) if *key*'s current spec.md parses to
    zero acceptance criteria -- see `set_phase`'s own docstring for why this
    is unconditional (no `force` override) and checked against the requested
    *phase*, not the source. A missing spec.md is not this gate's problem
    (nothing to check); some other, unrelated failure will surface that."""
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return
    if not snap_mod.has_acs(spec_path.read_text(encoding="utf-8")):
        raise store.MaestroError(
            f"{key}: refusing {phase.value} — spec.md has no acceptance criteria "
            f"(its '## Acceptance criteria' section parses to zero non-blank "
            f"'- [ ] ...' lines); add at least one AC first")


def _acs_unverified_count(cfg: Config, key: str, snap) -> int:
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return 0
    tree_key = _current_tree_key(cfg, key) if _annotations_active(cfg, key) else None
    return snap.acs_unverified(spec_path.read_text(encoding="utf-8"), tree_key=tree_key)


def _refuse_if_qa_failing(cfg: Config, key: str, snap) -> None:
    """Block `implementing -> awaiting-ci` while an independent *spec-axis* QA
    verdict on a current AC is still `fail` — the enforced half of the
    adversarial loop: a failing verdict must send the ticket back to
    `implementing`, not let it coast onward to review. Raises (no event
    appended) rather than warning, so a reconciler that tries anyway gets a
    hard, actionable stop.

    Deliberately checks `qa_failing_acs` (spec axis) only, never
    `standards_failing_acs` (T-23) — a Standards-axis fail is advisory and
    must NOT gate this transition; that is an explicit, tested choice, not an
    oversight (see tests/test_standards_qa_axis.py)."""
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return
    failing = snap.qa_failing_acs(spec_path.read_text(encoding="utf-8"))
    if failing:
        raise store.MaestroError(
            f"{key}: refusing awaiting-ci — QA verdict is fail on {len(failing)} "
            f"acceptance criteria: {'; '.join(failing)} — fix and re-run `maestro qa-verdict`")


def _refuse_if_qa_incomplete(cfg: Config, key: str, snap) -> None:
    """T-85: block `implementing -> awaiting-ci` unless every current-hash AC carries a
    PASSING spec-axis QA verdict -- stricter than `_refuse_if_qa_failing` just above,
    which only blocks a recorded *fail* (zero verdicts used to satisfy it silently, the
    bug this ticket closes). Config-gated by `cfg.awaiting_ci_qa_gate` (default ON) so a
    board can revert to HEAD's weaker check; unconditional (no `force` override) when
    live, same posture as `_refuse_if_qa_failing`."""
    if not cfg.awaiting_ci_qa_gate:
        return
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return
    unpassed = snap.qa_unpassed_acs(spec_path.read_text(encoding="utf-8"))
    if unpassed:
        raise store.MaestroError(
            f"{key}: refusing awaiting-ci — {len(unpassed)} acceptance criteria have no "
            f"passing spec-axis QA verdict: {'; '.join(unpassed)} — run `maestro qa-verdict "
            f"--verdict pass` for each from the qa phase")


def _tests_stale_reason(cfg: Config, key: str, snap) -> str | None:
    """None if the RB-12/RB-14 test-run gate is already satisfied for *key*
    right now -- in which case `set_phase` lets `implementing -> qa` through
    directly, with no `verifying` detour; otherwise the human-facing reason,
    and `set_phase` redirects to `verifying` instead so the dispatcher can run
    the suite itself. Satisfied means any of: the gate is unconfigured for
    *key* (T-83: no `test_command` resolves for it — its repo's own override
    or the board-wide default — ships dark, see `Config.test_command`), *key*
    is bound `mode: local` (AD-6, no repo/suite to run), or the CURRENT tree
    state (HEAD sha + a hash of the dirty tree, see `_tree_state_key`) already
    has a captured PASSING record in `snap.test_runs` — a real subprocess
    actually ran and recorded (`capture_tests`, or `dispatcher.sync_test_runs`'s
    async equivalent), never a free-text claim (that's the whole point: see
    `Snapshot.tests_passing`). A record from a stale tree state (one more edit
    since it was captured) is invisible here — it simply isn't keyed under the
    current tree_key — so it can never satisfy this check."""
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if not binding.test_command:
        return None
    if binding.mode == "local":
        return None
    from .dispatcher import _worker_cwd  # lazy: avoid a module-load cycle, mirrors qa_brief

    tree_key = _tree_state_key(_worker_cwd(cfg, key))
    if snap.tests_passing(tree_key):
        return None
    rec = snap.test_runs.get(tree_key)
    if rec is None:
        return "no captured test run matches the current tree state"
    return f"captured test run at this tree state failed (exit {rec.get('exit_code')})"


def _warn_unverified_acs(cfg: Config, key: str, *, actor: str) -> None:
    """Soft-warn (a Note event) when entering awaiting-ci with unattested ACs left
    (only reachable via `force`, since the gate above otherwise refuses first)."""
    snap = snap_mod.load(cfg.home, key)
    n = _acs_unverified_count(cfg, key, snap)
    if n <= 0:
        return
    _append(cfg, key, E.NOTE,
            {"text": f"{n} acceptance criteria unverified — run `maestro verify-ac` before merge"},
            actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, "warn-acs-unverified"))


# Structured evidence required by `verify_ac` — enough that a reviewer can tell
# what was actually run without re-deriving it from a free-text sentence.
EVIDENCE_FIELDS = ("what", "where", "result")


def _validate_evidence(evidence: dict) -> None:
    if not isinstance(evidence, dict):
        raise store.MaestroError(
            f"evidence must be structured with fields {EVIDENCE_FIELDS}, got {type(evidence).__name__}")
    missing = [f for f in EVIDENCE_FIELDS if not str(evidence.get(f, "")).strip()]
    if missing:
        raise store.MaestroError(
            f"evidence missing required field(s): {', '.join(missing)} (need {', '.join(EVIDENCE_FIELDS)})")


_LOCAL_BACKUP_KIND = "local-write-backup"
_LOCAL_BACKUP_TEXT = "local write backup: "


def _is_local_backup_note(ev: dict) -> bool:
    """A Note this op appended -- `kind` on everything it writes now, text
    prefix for the notes recorded before the marker existed."""
    if ev.get("type") != E.NOTE:
        return False
    payload = ev.get("payload") or {}
    return (payload.get("kind") == _LOCAL_BACKUP_KIND
            or str(payload.get("text", "")).startswith(_LOCAL_BACKUP_TEXT))


def local_write_backup(cfg: Config, key: str, *, actor: str = "reconciler",
                       now: float | None = None) -> str | None:
    """AD-6: snapshot *key*'s resolved target directory before the reconciler
    writes into it in place -- the compensating control a ``mode = "local"``
    repo binding uses in place of the PR review checkpoint a git binding gets.
    No-op unless the ticket resolves to a local-mode binding with an existing
    path (returns None); on a repeat call within the step it returns the
    archive the step already took.

    Idempotent per reconcile step: the backup is recorded under a step-id keyed
    on ``(phase, observed_seq)``, so a crash-and-respawn mid-step (after the
    tarball was already taken) does not create a second one -- it just resumes
    writing against the same backup.

    The step's ``observed_seq`` is the log's high-water mark IGNORING this op's
    own backup Notes -- NOT ``snapshot.observed_seq``, which its own append
    advances. Keying on the snapshot made the second call within a step compute
    a *different* step id and take a second tarball (masked whenever both
    landed inside one timestamp second and so shared a filename).
    """
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode != "local" or not binding.path:
        return None
    target = Path(binding.path)
    if not target.exists():
        return None
    events = event_log.read(cfg.home, key)
    observed_seq = max((e.get("seq", 0) for e in events if not _is_local_backup_note(e)),
                       default=0)
    sid = step_id(key, snap_mod.load(cfg.home, key).phase, observed_seq, _LOCAL_BACKUP_KIND)
    for e in events:
        if e.get("step_id") == sid:  # already backed up this step
            return (e.get("payload") or {}).get("archive")
    now = now if now is not None else store.now_epoch()
    archive = backup.backup_local_target(target, now)
    _append(cfg, key, E.NOTE,
            {"text": f"{_LOCAL_BACKUP_TEXT}{archive}", "kind": _LOCAL_BACKUP_KIND,
             "archive": str(archive)},
            actor=actor, sid=sid)
    return str(archive)


# ---------------------------------------------------------------------------
# GA-20: idempotent worktree create-or-adopt + config-declared `prime`.
#
# Call ONLY from a reconciler session (ready.md's `maestro worktree ensure`) --
# NEVER from dispatch(). `prime` is arbitrary shell read from config.toml, and
# the dispatcher runs as an unsandboxed launchd LaunchAgent with no session
# sandbox; MR-3 deliberately kept its own git work to removal-on-merge. Nothing
# in this module's dispatcher-facing code path (dispatcher.py) calls any
# function below.
# ---------------------------------------------------------------------------

_FETCH_TIMEOUT = 60     # seconds; a hung `git fetch` must never wedge a reconciler
_GIT_TIMEOUT = 30       # seconds; fast rev-parse/status plumbing calls only -- NOT
                        # `worktree add`/adopt itself, see `worktree_timeout` (MTO-1: a
                        # ~230k-file monorepo checkout measured ~56s, well past this)
# T-90: no more hard-coded prime default here -- `worktree_ensure`'s `prime_timeout`
# kwarg now defaults to None and falls back to the resolved binding's own
# `prime_timeout` (config-declared, board-wide + per-repo), the same shape as
# `worktree_timeout` below.

# GA-7-derived: gitignored-by-convention names a fresh `git worktree add` never
# brings (tracked files only) that this op mirrors from the source checkout.
_PRIMED_EXTRA_FILES = ("CLAUDE.local.md", ".claude/settings.local.json")
_PRIMED_NODE_MODULES = "node_modules"
_NODE_MODULES_SYNCED_MARKER = "maestro-node-modules-synced"

# MTO-1: staged/unstaged deletions at or above this count reads as a truncated index (the
# 2026-08-10 incident: 229,695), never a legitimate small in-flight delete a human or agent
# is mid-committing inside the worktree.
_MASS_DELETE_THRESHOLD = 50

# T-81: the positive completion witness. Written under the worktree's OWN git dir (see
# `_git_dir`, same posture as `_NODE_MODULES_SYNCED_MARKER`) ONLY after
# `_worktree_create_or_adopt` returns successfully -- never inferred from `worktree_health`,
# which is a heuristic (an ordinary mid-refactor `rm` of 50+ tracked files trips the same
# "mass deletion" signature an interrupted checkout does) and cannot tell "never finished
# creating" apart from "a live worktree in a state the heuristic dislikes". Teardown in
# `worktree_ensure` is gated on this marker's ABSENCE, not on `worktree_health`'s verdict --
# see that function's docstring.
_WORKTREE_COMPLETE_MARKER = "maestro-worktree-complete"


def _run_recovery_git(args: list[str], *, timeout: int, what: str) -> subprocess.CompletedProcess:
    """`subprocess.run` for the worktree-recovery path (`_cleanup_partial_worktree` and both
    `worktree_health` probes) -- *timeout* is always the caller's `worktree_timeout`, never the
    short plumbing `_GIT_TIMEOUT`, and a `TimeoutExpired`/`OSError` is wrapped into
    `store.MaestroError` here rather than left to escape as a raw traceback (T-81 finding 2:
    every call on this path used to run under 30s, uncaught, so cleanup itself could time out
    and leave a poison-pill directory behind)."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise store.MaestroError(
            f"{what} did not complete within {timeout}s ({exc}) -- raise `worktree_timeout` in "
            f"config.toml if this repo's git operations legitimately take longer.") from exc


def _git_dir(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> Path:
    """The worktree-SPECIFIC git dir (e.g. ``<repo>/.git/worktrees/<KEY>``) --
    distinct from ``--git-common-dir``/``--git-path``, which resolve to the
    dir SHARED by every worktree of the repo. Self-cleaning: `git worktree
    remove` (dispatcher.py's merge-triggered cleanup) deletes this whole
    directory, so anything stored under it never outlives its worktree.

    *timeout* defaults to the short plumbing `_GIT_TIMEOUT` for call sites
    that only ever run after a worktree is already known-good (marker writes
    in `_worktree_create_or_adopt`/`_prime_worktree_extras`). Callers on
    `worktree_ensure`'s hot path (`_worktree_witness_path`, checked on every
    `ensure` before any recovery logic runs) pass `cfg.worktree_timeout`
    instead, and a `TimeoutExpired`/`OSError` here is wrapped into
    `store.MaestroError` rather than left to escape as a raw traceback --
    T-81 AC5, round 2: this call used to be unguarded on that same hot path,
    reintroducing the exact poison-pill-timeout bug class the rest of this
    module's recovery path already closed via `_run_recovery_git`. A non-zero
    exit (not a worktree at all) still raises `CalledProcessError`, unchanged
    -- callers that treat "not a worktree" as a legitimate answer (e.g.
    `_worktree_witness_path`) catch that themselves."""
    try:
        out = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-dir"],
                             capture_output=True, text=True, timeout=timeout, check=True).stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise store.MaestroError(
            f"`git rev-parse --git-dir` on {wt} did not complete within {timeout}s ({exc}) -- "
            f"raise `worktree_timeout` in config.toml if this repo's git operations legitimately "
            f"take longer.") from exc
    p = Path(out)
    return p if p.is_absolute() else wt / p


def _has_prior_implementing_history(cfg: Config, key: str) -> bool:
    """True iff *key*'s own event log already recorded a `PhaseChanged` into
    `implementing` (archive included, so it survives a compaction) -- the
    signal that an adopt-fallback branch plausibly belongs to THIS ticket's
    current run, not a stale branch left behind by an earlier incarnation
    that merely reused this key (T-44)."""
    return any(
        e.get("type") == E.PHASE_CHANGED
        and (e.get("payload") or {}).get("phase") == Phase.IMPLEMENTING.value
        for e in event_log.read(cfg.home, key)
    )


def _worktree_has_index(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> bool:
    """True iff `git -C wt rev-parse --git-path index` resolves to a file that
    actually exists on disk. False for anything that isn't a usable git
    worktree at all: not-a-worktree, killed rev-parse (non-zero exit), missing
    index -- every one of those means "not safe to treat as complete". A
    `TimeoutExpired`/`OSError` running the probe itself is NOT one of those --
    it means the check never got an answer, not that the worktree is
    unhealthy (T-81 AC5), so it raises `store.MaestroError` (via
    `_run_recovery_git`) rather than silently returning False."""
    result = _run_recovery_git(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "index"],
        timeout=timeout, what=f"health probe (`git rev-parse --git-path index` on {wt})")
    if result.returncode != 0:
        return False
    out = result.stdout.strip()
    if not out:
        return False
    p = Path(out)
    if not p.is_absolute():
        p = wt / p
    return p.exists()


def _worktree_status_clean_enough(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> bool:
    """False iff `git status --porcelain` carries a mass deletion block (>=
    `_MASS_DELETE_THRESHOLD` lines whose worktree-status column is `D`) -- the
    signature of a truncated index (files present on disk, index believes
    them gone). A probe timeout raises `store.MaestroError` rather than
    silently returning False -- see `_worktree_has_index`."""
    result = _run_recovery_git(
        ["git", "-C", str(wt), "status", "--porcelain"],
        timeout=timeout, what=f"health probe (`git status --porcelain` on {wt})")
    if result.returncode != 0:
        return False
    deletions = sum(1 for line in result.stdout.splitlines() if line[1:2] == "D")
    return deletions < _MASS_DELETE_THRESHOLD


def worktree_health(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> dict:
    """The postcondition for "is *wt* a complete, usable worktree" -- `maestro
    doctor`'s detective check (health.py) and `worktree_ensure`'s
    witnessed-but-unhealthy refusal (see that function's docstring; NOT its
    teardown gate -- that's the completion witness, `_WORKTREE_COMPLETE_MARKER`).
    Bare directory existence (the pre-MTO-1 check) says nothing about whether
    `git worktree add` actually finished; an interrupted one can leave the
    directory behind with no index at all, or an index that never got the
    checkout it describes -- `git status` then reports every tracked file as a
    staged deletion. Two independent probes because MTO-1's one root cause (an
    interrupted `worktree add`) produced both symptoms depending on exactly
    when it was killed.

    *timeout* should be the caller's `worktree_timeout`, not the default short
    `_GIT_TIMEOUT` (T-81 AC3) -- callers with a `Config` in hand (both call
    sites do) should pass `cfg.worktree_timeout` explicitly.

    Returns ``{"healthy": bool, "reason": str | None}``. Unlike before T-81,
    this CAN raise `store.MaestroError` if a probe itself times out or errors
    -- a probe timeout is not evidence of an unhealthy worktree and must never
    be silently folded into "unhealthy" (AC5); `health.check_worktree_health`
    catches this per-worktree so one slow probe can't crash `maestro doctor`."""
    if not wt.exists():
        return {"healthy": False, "reason": "missing"}
    if not _worktree_has_index(wt, timeout=timeout):
        return {"healthy": False, "reason": "no index"}
    if not _worktree_status_clean_enough(wt, timeout=timeout):
        return {"healthy": False, "reason": "mass deletion in git status"}
    return {"healthy": True, "reason": None}


def _cleanup_partial_worktree(repo: str, wt: Path, *, branch: str | None = None,
                              base: str | None = None, timeout: int) -> None:
    """Tear down *wt* regardless of how far its creation got -- git may or may
    not have finished registering it. `git worktree remove --force` first
    (the clean path: deregisters the repo's own worktree admin dir too, not
    just the directory); if git itself doesn't consider *wt* a worktree yet
    (e.g. `worktree add` was killed before it linked anything), fall back to a
    raw `rmtree`. Either way, finish with `worktree prune` so a half-linked
    admin entry never makes the next `worktree add` for the same path fail
    with "already exists".

    Callers only ever reach this when the completion witness
    (`_WORKTREE_COMPLETE_MARKER`) is ABSENT -- i.e. *wt*'s creation never
    finished -- never on a witnessed, completed worktree just because
    `worktree_health` dislikes its current state (T-81).

    *timeout* is the caller's `worktree_timeout` (T-81 AC3) -- every call here
    used to run under the short 30s `_GIT_TIMEOUT`, uncaught, so on a large
    repo the cleanup itself could time out and leave a poison-pill directory
    behind (finding 2); `_run_recovery_git` now bounds each call by *timeout*
    and wraps a `TimeoutExpired`/`OSError` into `store.MaestroError`.

    When *branch*/*base* are given, ALSO discards the local *branch* ref if it
    carries zero commits beyond ``origin/<base>`` -- indistinguishable from a
    branch `worktree add -b` just created (fast) moments before being killed
    mid-checkout (slow), never touched since. Left alone otherwise: a branch
    WITH real commits ahead is the genuine T-44 resume case
    (`_worktree_create_or_adopt`'s adopt fallback exists for exactly that) and
    must never be discarded here. This is what lets the very next
    `_worktree_create_or_adopt` call recreate with a plain `-b` instead of
    colliding with its own interrupted attempt's leftover empty branch and
    falling through to the T-44 guard, which would then correctly-but-
    unhelpfully refuse (no prior `implementing` history yet, this early)."""
    removed = _run_recovery_git(["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
                                timeout=timeout, what=f"`git worktree remove --force {wt}`")
    if removed.returncode != 0 and wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    _run_recovery_git(["git", "-C", repo, "worktree", "prune"],
                      timeout=timeout, what="`git worktree prune`")
    if branch and base:
        ahead = _run_recovery_git(
            ["git", "-C", repo, "rev-list", "--count", f"origin/{base}..{branch}"],
            timeout=timeout, what=f"`git rev-list --count origin/{base}..{branch}`")
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            _run_recovery_git(["git", "-C", repo, "branch", "-D", branch],
                              timeout=timeout, what=f"`git branch -D {branch}`")


def _worktree_create_or_adopt(cfg: Config, key: str, repo: str, wt: Path, branch: str, base: str,
                              *, timeout: int) -> None:
    """Create *wt* on a fresh branch off ``origin/<base>``, or adopt *branch*
    if it already exists -- the same create-then-fallback the old ready.md
    prose did. Raises loudly (never a silent no-op) if both attempts fail.

    *timeout* bounds each `worktree add`/adopt attempt (`worktree_timeout`,
    config-declared, NOT the short `_GIT_TIMEOUT` used for plumbing calls
    elsewhere in this module -- MTO-1: a real monorepo checkout took ~56s).
    A `TimeoutExpired` on the CREATE attempt is caught, the partial worktree
    AND its just-orphaned empty branch cleaned up (`_cleanup_partial_worktree`,
    branch-pruning armed only here -- never on the adopt attempt below, which
    exists specifically to preserve a branch's real commits), and re-raised as
    `store.MaestroError` -- never left to escape as a raw traceback that
    leaves a poison-pill directory for every retry to skip past.

    The adopt fallback exists ONLY to resume a branch THIS TICKET'S CURRENT RUN
    already pushed commits to (its worktree dir got removed, the branch
    survives) -- never to bind a fresh incarnation of a reused key to some
    stranger's history. Ticket keys are reused across board incarnations, and
    a repo accumulates `maestro/<key>` branches from all of them (T-44: this
    repo alone carries 100+); adopting one on nothing but a name match can
    silently check out a tree that predates today's tooling entirely. So
    before adopting, this requires `_has_prior_implementing_history` --
    proof *this* run has been in `implementing` before -- and refuses
    (`store.MaestroError`, no event appended, non-zero exit) naming the
    branch and telling the operator to archive/rename it, rather than
    adopting-and-hoping. On success (create or adopt), writes the completion
    witness (`_WORKTREE_COMPLETE_MARKER`, under *wt*'s own git dir) that
    `worktree_ensure` gates teardown on -- never before this point, so a
    crash/timeout anywhere above leaves no witness behind (T-81)."""
    try:
        fetch = subprocess.run(["git", "-C", repo, "fetch", "-q", "origin", base],
                               capture_output=True, text=True, timeout=_FETCH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise store.MaestroError(
            f"git fetch origin {base!r} in {repo} did not complete within {_FETCH_TIMEOUT}s "
            f"({exc})") from exc
    if fetch.returncode != 0:
        raise store.MaestroError(
            f"git fetch origin {base!r} in {repo} failed (rc={fetch.returncode}): "
            f"{fetch.stderr.strip()}")

    def _add(args: list[str], attempt: str, *, prune_branch: bool) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["git", "-C", repo, "worktree", "add", *args],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _cleanup_partial_worktree(repo, wt, branch=branch if prune_branch else None,
                                      base=base if prune_branch else None, timeout=timeout)
            raise store.MaestroError(
                f"{key}: `git worktree add` ({attempt}) exceeded its {timeout}s "
                f"worktree_timeout in {repo} -- killed and cleaned up the partial worktree "
                f"at {wt}; raise `worktree_timeout` in config.toml if this repo's checkout "
                f"legitimately takes longer.")

    created = _add([str(wt), "-b", branch, f"origin/{base}"], "create", prune_branch=True)
    if created.returncode != 0:
        if not _has_prior_implementing_history(cfg, key):
            raise store.MaestroError(
                f"{key}: refusing to adopt existing branch {branch!r} in {repo} -- {key}'s event "
                f"log has no prior `implementing` phase, so this branch cannot be this run's own "
                f"history (create off origin/{base} failed: {created.stderr.strip()}). It is "
                f"almost certainly a stale branch left by an earlier ticket that reused this key "
                f"-- archive or rename it (e.g. `git -C {repo} branch -m {branch} "
                f"archive/{branch}`) and re-run `maestro worktree ensure {key}`.")
        adopted = _add([str(wt), branch], "adopt", prune_branch=False)
        if adopted.returncode != 0:
            raise store.MaestroError(
                f"git worktree add failed for {wt} in {repo}: create "
                f"({created.stderr.strip()}) and adopt ({adopted.stderr.strip()}) both failed")

    witness = _git_dir(wt) / _WORKTREE_COMPLETE_MARKER
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.write_text(store.iso_now() + "\n", encoding="utf-8")


def _prime_worktree_extras(repo: str, wt: Path, *, timeout: int = _GIT_TIMEOUT) -> None:
    """GA-7, absorbed: exclude the mirrored names from `git add` via the
    git-COMMON `info/exclude` (idempotent -- shared across every worktree of
    the repo), then mirror CLAUDE.local.md / .claude/settings.local.json /
    node_modules from the source checkout into *wt*. A real, write-isolated
    copy (never a symlink or hardlink -- see the `cp` ladder below), so an
    install run inside *wt* never writes through into *repo* or a sibling
    worktree. Every step is a no-op when the source doesn't have it.

    T-95: *timeout* should be the caller's `worktree_timeout`, not the
    default short `_GIT_TIMEOUT` -- `worktree_ensure` passes it explicitly.
    The leading `git rev-parse --git-path info/exclude` used to run
    unguarded (short-timeout `subprocess.run(..., check=True)`) on
    `worktree_ensure`'s hot path, so a hung or failing git there escaped as a
    raw `subprocess.CalledProcessError` traceback instead of a loud
    `store.MaestroError` -- `cli.main` only catches the latter (T-81's own
    finding, missed here). Routed through `_run_recovery_git` like the rest
    of the recovery path."""
    result = _run_recovery_git(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        timeout=timeout, what=f"`git rev-parse --git-path info/exclude` on {wt}")
    if result.returncode != 0:
        raise store.MaestroError(
            f"`git rev-parse --git-path info/exclude` on {wt} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}")
    exclude_path = Path(result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = wt / exclude_path
    existing = (exclude_path.read_text(encoding="utf-8").splitlines()
               if exclude_path.exists() else [])
    names = _PRIMED_EXTRA_FILES + (f"{_PRIMED_NODE_MODULES}/",)
    missing = [n for n in names if n not in existing]
    if missing:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a", encoding="utf-8") as f:
            for n in missing:
                f.write(n + "\n")

    repo_path = Path(repo)
    for name in _PRIMED_EXTRA_FILES:
        src = repo_path / name
        if src.exists():
            dst = wt / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    src_nm = repo_path / _PRIMED_NODE_MODULES
    dst_nm = wt / _PRIMED_NODE_MODULES
    if src_nm.is_dir():
        # MTO-1: gated by a completion marker written ONLY after the sync returns 0 --
        # never by `dst_nm.exists()`, which a partial copy (killed/timed-out mid-run)
        # already satisfies, so every retry used to skip it forever (the 2.6-of-5.2 GB
        # truncation this ticket reproduced). The marker lives under the worktree's OWN
        # git dir (see `_git_dir`), same as `maestro-primed` below, so it self-cleans on
        # `git worktree remove`.
        #
        # rsync -a preserves the write-isolated, real-copy guarantee the old `cp` ladder
        # gave (never a symlink/hardlink -- an in-place write inside *wt* must never reach
        # *repo* or a sibling worktree); unlike a one-shot `cp`, it is also idempotent and
        # resumable -- a re-run only transfers what's missing or size/mtime-mismatched, so
        # completing a partial copy from a prior kill is a normal, cheap re-run rather than
        # a full re-copy. `--delete` clears anything rsync's own check finds truncated.
        # Trailing "/" on both sides syncs node_modules' *contents* into place instead of
        # nesting a second node_modules inside it.
        marker = _git_dir(wt) / _NODE_MODULES_SYNCED_MARKER
        if not marker.exists():
            dst_nm.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(["rsync", "-a", "--delete", f"{src_nm}/", f"{dst_nm}/"],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise store.MaestroError(
                    f"node_modules sync into {dst_nm} failed (rsync rc={result.returncode}): "
                    f"{result.stderr.strip()}")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(store.iso_now() + "\n", encoding="utf-8")


def _run_prime(binding: repos_mod.RepoBinding, wt: Path, repo: str, key: str, timeout: int) -> None:
    env = {**os.environ, "WT": str(wt), "REPO": repo, "KEY": key}
    try:
        result = subprocess.run(binding.prime, shell=True, cwd=wt, env=env,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise store.MaestroError(
            f"prime for repo {binding.name!r} ({key}) exceeded its {timeout}s prime_timeout -- "
            f"raise `prime_timeout` in config.toml ([maestro] board-wide, or "
            f"[repos.{binding.name}] to override just this repo) if this repo's cold "
            f"dependency install legitimately takes longer.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise store.MaestroError(
            f"prime for repo {binding.name!r} ({key}) failed (rc={result.returncode}): {detail}")


def _worktree_witness_path(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> Path | None:
    """Path to *wt*'s completion witness (`_WORKTREE_COMPLETE_MARKER`), or
    None if *wt* isn't even a usable git worktree yet -- in which case no
    witness could possibly exist. A directory that exists but isn't a real
    git worktree (no index at all, T-44/MTO-1's "no index" repro) makes
    `_git_dir`'s `rev-parse --git-dir` fail non-zero (`check=True`,
    `CalledProcessError`), which reads the same as "no witness" here.

    *timeout* should be `cfg.worktree_timeout` -- this runs unconditionally
    on `worktree_ensure`'s hot path, before any recovery logic, so a
    `TimeoutExpired`/`OSError` probing it is NOT "no witness": that would
    make `worktree_ensure` mistake "we don't know" for "creation never
    finished" and tear down a live, witnessed worktree -- the exact failure
    this ticket exists to close. `_git_dir` wraps that case into
    `store.MaestroError`, which is deliberately NOT caught here and
    propagates up as a loud, non-zero-exit failure instead (T-81 AC5,
    round 2)."""
    if not wt.exists():
        return None
    try:
        return _git_dir(wt, timeout=timeout) / _WORKTREE_COMPLETE_MARKER
    except subprocess.CalledProcessError:
        return None


def worktree_witness_status(wt: Path, *, timeout: int = _GIT_TIMEOUT) -> dict:
    """T-95: the one predicate that decides whether a witness-less *wt* is a
    pre-T-81 worktree (backfill and treat as complete) or an interrupted
    checkout (still torn down) -- shared by `worktree_ensure`'s write path and
    `health.check_worktree_witness`'s read-only doctor report, so the two
    never drift apart.

    Returns ``{"witnessed": bool, "backfillable": bool, "witness_path": Path | None}``:
      - `witnessed` True: the completion witness already exists on disk --
        nothing to do.
      - `witnessed` False, `backfillable` True: *wt* IS a registered git
        worktree with a real index (`_worktree_has_index`) but has never
        carried a witness -- it predates T-81 (nothing ever wrote one for it),
        not an interrupted `worktree add`. Safe to backfill.
      - `witnessed` False, `backfillable` False: either *wt* isn't a usable
        git worktree at all (`_worktree_witness_path` returned None) or it is
        one but has no index -- MTO-1's interrupted-checkout signature. Must
        still be torn down and rebuilt, never backfilled.
    `witness_path` is the path a witness would live at (or already does),
    None when *wt* isn't even a usable git worktree yet.
    """
    witness = _worktree_witness_path(wt, timeout=timeout)
    if witness is None:
        return {"witnessed": False, "backfillable": False, "witness_path": None}
    if witness.exists():
        return {"witnessed": True, "backfillable": False, "witness_path": witness}
    backfillable = _worktree_has_index(wt, timeout=timeout)
    return {"witnessed": False, "backfillable": backfillable, "witness_path": witness}


def worktree_ensure(cfg: Config, key: str, *, prime_timeout: int | None = None) -> dict:
    """Idempotently create-or-adopt *key*'s reconciler worktree, absorb GA-7's
    CLAUDE.local.md/.claude/settings.local.json/node_modules priming, and run
    the resolved repo binding's `prime` command exactly once inside it.

    `prime` comes ONLY from ``repos.resolve()`` (config.toml's ``[repos.<name>]
    prime`` or the ``[maestro] prime`` fallback) -- never a spec front-matter
    field or a ``TicketCreated`` payload, neither of which this function or
    anything it calls ever reads.

    T-90: *prime_timeout* defaults to None, which resolves to the same binding's
    ``prime_timeout`` (config-declared, ``[repos.<name>] prime_timeout`` or the
    ``[maestro] prime_timeout`` fallback -- the same precedence as `prime` itself).
    An explicit kwarg (as the CLI never passes, but a caller/test may) still wins
    over that resolved value. `worktree_timeout` is resolved from the same binding
    too, rather than read off *cfg* directly, so a per-repo override of either
    knob actually reaches its own subprocess.

    Idempotence (T-81, replacing the old `worktree_health`-driven gate): a
    fresh ``git worktree add`` is skipped once *wt* carries the completion
    witness written by `_worktree_create_or_adopt` on its last successful
    run -- NOT once `worktree_health` merely finds the worktree "healthy".
    `worktree_health` is a heuristic (an ordinary mid-refactor `rm` of 50+
    tracked files trips the same "mass deletion" signature a truncated
    checkout does) and cannot tell "this worktree's creation never finished"
    apart from "this is a live worktree in a state the heuristic dislikes" --
    conflating the two used to mean a live worktree holding uncommitted work
    got silently `--force` removed and reported `{"created": True}` on exit 0
    (T-81 finding 1). So (`worktree_witness_status`):
      - Witnessed already -> real no-op path below, as before.
      - Witness-less but `backfillable` (a registered worktree with a real
        index that has simply never carried a witness -- T-95: every
        worktree created before T-81 shipped looks like this, since nothing
        ever backfilled one) -> the witness is written right here, and
        *wt* falls through to the SAME witnessed/health-checked branch
        below as if it had always had one -- never torn down just for
        predating the marker.
      - Witness-less and NOT backfillable (`_worktree_witness_path` found no
        usable git worktree at *wt* at all, or one with no index) -- creation
        genuinely never finished (or *wt* doesn't exist) -- torn down
        (`_cleanup_partial_worktree`, safe: there is nothing completed to
        lose) and recreated.
      - Witnessed (or just backfilled), `worktree_health` healthy -> real
        no-op.
      - Witnessed (or just backfilled), `worktree_health` UNHEALTHY ->
        creation genuinely finished but the worktree now looks wrong (mass
        deletion, no index anymore, ...) -- likely carrying uncommitted work.
        Ensure REFUSES (`store.MaestroError`, no event appended, non-zero CLI
        exit) naming the worktree and the remedy, rather than guessing and
        possibly discarding it; a human inspects and either fixes it in place
        or removes it themselves before re-running `ensure`.
    `prime` is skipped once its own worktree-local marker (under the
    worktree's OWN git dir -- see `_git_dir` -- so it self-cleans when the
    worktree is removed) is present. If a fresh branch can't be created (it
    already exists), the adopt fallback (see `_worktree_create_or_adopt`)
    only fires when *key*'s own event log shows it was already `implementing`
    before -- otherwise it refuses (`store.MaestroError` naming the branch,
    T-44) rather than silently binding this run to a stale branch left by an
    earlier ticket that reused this key. Raises ``store.MaestroError`` --
    never a silent success -- if worktree add/adopt fails or times out
    (`worktree_timeout`), or if `prime` exits non-zero or exceeds
    *prime_timeout*; the CLI wrapper surfaces that as a non-zero exit with the
    repo name and rc in stderr, and appends no event, so no phase transition
    follows a failed ensure.

    AD-6 ``mode = "local"`` bindings have no worktree at all: a successful,
    side-effect-free no-op.
    """
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode == "local":
        return {"created": False, "primed": False}
    if not binding.path:
        raise store.MaestroError(f"{key}: repo binding {binding.name!r} has no path configured")

    # T-90: resolved from the binding (per-repo override, else the board-wide
    # default), NOT read off cfg directly -- so a [repos.<name>] override of
    # either knob actually reaches its own subprocess. An explicit kwarg still
    # wins over the resolved worktree_timeout.
    worktree_timeout = binding.worktree_timeout
    effective_prime_timeout = prime_timeout if prime_timeout is not None else binding.prime_timeout

    repo = binding.path
    wt = store.worktree_path(cfg.home, key)
    branch = f"{binding.branch_prefix}{key}"
    status = worktree_witness_status(wt, timeout=worktree_timeout)
    if status["backfillable"]:
        # T-95: a registered git worktree with a real index but no witness --
        # this worktree's creation genuinely finished, it simply predates
        # T-81 (nothing ever wrote one for it). Backfill and fall through to
        # the witnessed/health-checked branch below instead of destroying it.
        witness = status["witness_path"]
        witness.parent.mkdir(parents=True, exist_ok=True)
        witness.write_text(store.iso_now() + "\n", encoding="utf-8")
        status = {**status, "witnessed": True}

    created = False
    if not status["witnessed"]:
        if wt.exists():
            _cleanup_partial_worktree(repo, wt, branch=branch, base=binding.base_branch,
                                      timeout=worktree_timeout)
        _worktree_create_or_adopt(cfg, key, repo, wt, branch, binding.base_branch,
                                  timeout=worktree_timeout)
        created = True
    else:
        health = worktree_health(wt, timeout=worktree_timeout)
        if not health["healthy"]:
            raise store.MaestroError(
                f"{key}: worktree at {wt} already completed creation but now fails its health "
                f"check ({health['reason']}) -- refusing to `--force` remove a worktree that may "
                f"be carrying uncommitted work. Inspect it by hand (e.g. `git -C {wt} status`); "
                f"if it is genuinely broken beyond repair, remove it yourself "
                f"(`git -C {repo} worktree remove --force {wt}`) and re-run "
                f"`maestro worktree ensure {key}`.")

    _prime_worktree_extras(repo, wt, timeout=worktree_timeout)

    primed = False
    if binding.prime:
        marker = _git_dir(wt) / "maestro-primed"
        if not marker.exists():
            _run_prime(binding, wt, repo, key, effective_prime_timeout)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(store.iso_now() + "\n", encoding="utf-8")
            primed = True

    return {"created": created, "primed": primed}


def verify_ac(cfg: Config, key: str, ac_index: int, evidence: dict, *, actor: str = "reconciler") -> str:
    """Attest AC #ac_index (1-based, in spec order) with structured evidence.

    `evidence` must have non-empty `what` (what was run), `where` (file:line or
    test name), and `result` (the observed outcome) fields — a call missing any
    of them is rejected before anything is appended.

    Identified by content hash of the AC's own spec line, not by index, so a
    human edit to that line invalidates the attestation (`acs_unverified` counts
    it again) instead of silently mismatching a different AC at the same index.
    """
    _validate_evidence(evidence)
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        raise store.MaestroError(f"{key}: no spec.md to verify ACs against")
    acs = snap_mod.parse_acs(spec_path.read_text(encoding="utf-8"))
    if not (1 <= ac_index <= len(acs)):
        raise store.MaestroError(f"{key}: AC #{ac_index} out of range (spec has {len(acs)} AC(s))")
    ac_text = acs[ac_index - 1]
    h = snap_mod.ac_hash(ac_text)
    _append(cfg, key, E.AC_VERIFIED,
            {"ac_hash": h, "ac_index": ac_index, "ac_text": ac_text, "evidence": evidence},
            actor=actor, sid=f"acverified-{key}-{h}")
    return h


QA_VERDICTS = {"pass", "fail"}
QA_AXES = {"spec", "standards"}


def _qa_brief_ac_entry(index: int, text: str, tree_key: str, snap) -> dict:
    """One `qa_brief` AC row (T-79): index/text/hash, unchanged, plus -- only
    for an annotated AC -- its annotation and latest current-tree captured
    check, if the verifying stage has already run one at this tree state."""
    h = snap_mod.ac_hash(text)
    entry = {"index": index, "text": text, "ac_hash": h}
    ann = snap_mod.parse_ac_annotation(text)
    if ann is not None:
        entry["annotation"] = {"kind": ann.kind, "raw": ann.raw}
        rec = snap.ac_check_record(tree_key, h)
        if rec is not None:
            entry["captured_check"] = rec
    return entry


def qa_brief(cfg: Config, key: str) -> dict:
    """Build the Implementer->QA hand-off packet for *key*, deterministically.

    Read-only: appends no event and mutates nothing, so it is safe to call on
    every QA round and safe to retry.

    The hand-off used to be assembled by the implementer itself -- run
    `git diff`, then re-type the AC list and that diff text into the QA
    sub-agent's prompt. That is the one step of the loop with no plumbing
    behind it, and a weaker model drops it: the sub-agent gets an empty or
    truncated diff and verdicts against nothing. Minting the packet here makes
    the briefing a CLI result the QA agent reads, not prose the implementer has
    to marshal -- the same reason `verify_ac`/`record_qa_verdict` own AC
    indexing rather than trusting an agent to count.

    The diff is taken in `dispatcher._worker_cwd` -- the ticket's worktree when
    one exists, else its resolved repo binding -- so the packet always describes
    the tree the reconciler is actually working in, and against the binding's
    own `base_branch` rather than an assumed "main".
    """
    from .dispatcher import _worker_cwd

    spec_file = store.spec_path(cfg.home, key)
    if not spec_file.exists():
        raise store.MaestroError(f"{key}: no spec.md to brief QA against")
    spec_text = spec_file.read_text(encoding="utf-8")
    # T-80: `has_acs` (not a bare `parse_acs` truthiness check) -- a spec whose
    # AC section parses to one entry with empty/whitespace-only text (the seed
    # template's own dangling "- [ ] ") must refuse here too, not just a spec
    # that parses to no entries at all; minting a QA packet with one blank AC
    # is exactly the empty-packet shape this refusal exists to prevent.
    if not snap_mod.has_acs(spec_text):
        raise store.MaestroError(f"{key}: spec.md has no acceptance criteria to check")
    acs = snap_mod.parse_acs(spec_text)

    binding = repos_mod.resolve(cfg, cfg.home, key)
    base = binding.base_branch
    cwd = _worker_cwd(cfg, key)
    diff, base_ref, ahead, stderr = _qa_diff(cwd, base)
    tree_key = _tree_state_key(cwd)
    snap = snap_mod.load(cfg.home, key)
    return {
        "key": key,
        # T-79: each AC's own `test:`/`check:` annotation (if any) plus its
        # latest CURRENT-tree captured check, so QA can audit test-vs-AC
        # fidelity -- does the named test's assertions actually match the AC's
        # words? -- instead of re-deriving that from the raw diff every time.
        # An unannotated AC carries neither key, unchanged from before.
        "acs": [_qa_brief_ac_entry(i, t, tree_key, snap) for i, t in enumerate(acs, start=1)],
        "base_ref": base_ref,
        "cwd": str(cwd),
        "diff": diff,
        "diff_empty": not diff.strip(),
        # Disambiguates the two very different states that both yield an empty
        # diff: nothing implemented yet (0 commits) vs. work already merged into
        # the base (>0 commits, content identical). A QA agent that can't tell
        # them apart verdicts "fail -- nothing changed" on finished work.
        "commits_ahead": ahead,
        "warning": stderr or None,
    }


def _qa_diff(cwd, base: str) -> tuple[str, str, int, str]:
    """``git diff`` for the QA packet, preferring ``origin/<base>`` over ``<base>``.

    Diffs against the MERGE-BASE, not the base tip and not ``<base>...HEAD``.
    Each of the obvious forms is wrong in a way that misleads QA:

    - ``git diff origin/<base> --`` (what the skill ran) compares the worktree to
      the base TIP, so every commit that landed on the base after branching leaks
      in as inverted noise. Measured on a real dogfood worktree: 72,573 bytes
      against 10,876 for the same one-commit branch.
    - ``git diff origin/<base>...HEAD`` excludes that noise but is a
      commit-to-commit diff, so it silently DROPS uncommitted work -- and
      mid-`implementing` the agent frequently has not committed yet. QA would be
      briefed with an empty diff on a tree full of changes.

    ``git diff <merge-base> --`` gets both: everything this branch changed,
    committed or not, with no base advancement.

    Returns ``(diff, base_ref_used, commits_ahead, stderr)``. A worktree with no
    fetched ``origin/<base>`` (a fresh clone, an offline box) falls back to the
    local branch, then to the base tip if no merge-base exists, rather than
    raising -- an empty diff with a warning routes back to `implementing`,
    whereas an exception would fail the whole reconcile step.
    """
    import subprocess

    def _git(*args):
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True)

    proc = None
    for ref in (f"origin/{base}", base):
        mb = _git("merge-base", ref, "HEAD")
        anchor = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else ref
        proc = _git("diff", anchor, "--")
        if proc.returncode == 0:
            count = _git("rev-list", "--count", f"{ref}..HEAD")
            ahead = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
            return proc.stdout + _untracked_diff(_git), ref, ahead, ""
    return "", base, 0, ((proc.stderr if proc else "") or "").strip()[:400]


# A brand-new file the implementer has not `git add`-ed yet is invisible to
# `git diff` at any anchor -- and "add the new module + its test" is the single
# most common shape of an implementing step. Briefing QA without them produces
# the exact false FAIL ("no test was added") this verb exists to prevent.
_UNTRACKED_FILE_CAP = 50


def _untracked_diff(_git) -> str:
    """Diff hunks for untracked, non-ignored files, appended to the packet.

    Uses ``git diff --no-index /dev/null <path>``, which renders a real
    add-file hunk WITHOUT touching the index -- `git add -N` would have made
    this verb a mutation. ``--no-index`` exits 1 when the files differ (the
    normal case here), so only exit >1 counts as an error.
    """
    listed = _git("ls-files", "--others", "--exclude-standard")
    if listed.returncode != 0:
        return ""
    paths = [p for p in listed.stdout.splitlines() if p.strip()]
    if not paths:
        return ""
    out = []
    for path in paths[:_UNTRACKED_FILE_CAP]:
        d = _git("diff", "--no-index", "--", "/dev/null", path)
        if d.returncode <= 1 and d.stdout:
            out.append(d.stdout)
    if len(paths) > _UNTRACKED_FILE_CAP:
        out.append(f"\n[maestro] {len(paths) - _UNTRACKED_FILE_CAP} further untracked "
                   f"file(s) omitted from this packet\n")
    return "".join(out)


# RB-12: how long a real `cfg.test_command` subprocess may run before
# `capture_tests` gives up and records it as a failure -- generous, same
# posture as `worktree_timeout` (MTO-1): a killed-mid-run suite must read as
# "failed", never wedge the reconciler that's waiting on it.
_TEST_RUN_TIMEOUT = 1800  # seconds


def _tree_state_key(cwd: Path) -> str:
    """"<HEAD sha>:<hash of the dirty tree>" at *cwd* -- the RB-12 gate's
    binding key, chosen so it changes exactly when the code does (the spec's
    "bind the record to the tree, not the ticket" note): a passing capture
    from before one more edit never matches after it. The dirty half covers
    both tracked changes (`git diff HEAD`) and untracked new files (the same
    `_untracked_diff` helper `_qa_diff` briefs QA with), so a brand-new,
    not-yet-`git add`-ed file still moves the key. A non-git *cwd* (rev-parse
    fails) degrades to an all-empty key rather than raising -- `capture_tests`
    is never called for a `mode: local` binding, so this is a defensive
    fallback, not an expected path.
    """
    def _git(*args):
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT)

    head = _git("rev-parse", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else ""
    dirty = _git("diff", "HEAD", "--")
    dirty_text = (dirty.stdout if dirty.returncode == 0 else "") + _untracked_diff(_git)
    return f"{sha}:{content_hash(dirty_text)}"


# RB-14: bounded tail of a failing run's combined stdout+stderr kept on its
# TestRunCaptured event -- generous enough to show the actual pytest failure
# (a traceback, an assertion diff), capped so a giant red log can never bloat
# the append-only event log the way an unbounded one would (spec Notes: "bound
# the size"). A passing run carries no excerpt at all -- nothing to show.
_EXCERPT_TAIL_CHARS = 4000


def _bounded_excerpt(output: str) -> str:
    if not output:
        return ""
    return output[-_EXCERPT_TAIL_CHARS:]


def _record_test_run(cfg: Config, key: str, *, tree_key: str, command: str,
                      exit_code: int, output: str, actor: str) -> dict:
    """Append one TestRunCaptured event -- the single place BOTH the
    synchronous, agent-invoked `capture_tests` below and the dispatcher-owned
    async runner (`dispatcher.sync_test_runs`/`_fold_test_run`, RB-14) build
    the payload, so the two callers can never drift on what fields a captured
    run carries. `output` is the run's combined stdout+stderr; only a bounded
    tail is kept, and only on a failing run (see `_bounded_excerpt`)."""
    passed = exit_code == 0
    payload = {"tree_key": tree_key, "command": command, "exit_code": exit_code, "passed": passed}
    if not passed:
        payload["failure_excerpt"] = _bounded_excerpt(output)
    _append(cfg, key, E.TEST_RUN_CAPTURED, payload, actor=actor,
            sid=f"testrun-{key}-{tree_key}")
    return payload


def capture_tests(cfg: Config, key: str, *, actor: str = "reconciler") -> dict:
    """Run *key*'s resolved `test_command` (T-83: its repo's own override, or
    the board-wide `[maestro] test_command` default -- see
    `repos.resolve(...).test_command`) for real -- a subprocess, ITS OWN exit code
    -- at *key*'s current tree state, and record the result as a
    TestRunCaptured event. This is the only thing `_tests_stale_reason`
    (consulted by `set_phase`'s `implementing -> qa`/`verifying` redirect)
    ever trusts; a Note, a `verify-ac`, or any other free-text claim of "tests
    pass" satisfies nothing (RB-12: "the evidence must be captured, never
    claimed"). RB-14 made calling this manually optional -- the dispatcher's
    own `sync_test_runs` tick now does the same thing asynchronously once a
    ticket reaches `verifying` -- but it stays available (and this caching
    rule keeps it cheap to call anyway, e.g. a human checking early).

    Caching -- the chosen rule for "do not run the suite on every sweep": if a
    record already exists for this exact tree_key (HEAD sha + a hash of the
    dirty tree, see `_tree_state_key`), the command is NOT re-run at all; the
    existing record is returned as-is. Only a tree state maestro has never
    captured before triggers a real subprocess call, so a matching tree state
    across repeated calls (e.g. two dispatcher sweeps back to back) costs
    exactly one suite run, not one per call.

    No-ops (`{"skipped": "..."}`, no event appended) when no `test_command`
    resolves for *key* (the gate is disabled for it) or *key*'s repo binding
    is `mode: local` (AD-6 -- a plain, non-git target with no suite to run).
    """
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if not binding.test_command:
        return {"skipped": "unconfigured"}
    if binding.mode == "local":
        return {"skipped": "local"}
    from .dispatcher import _worker_cwd  # lazy: avoid a module-load cycle, mirrors qa_brief

    cwd = _worker_cwd(cfg, key)
    tree_key = _tree_state_key(cwd)
    snap = snap_mod.load(cfg.home, key)
    cached = snap.test_runs.get(tree_key)
    if cached is not None:
        return {**cached, "tree_key": tree_key, "cached": True}
    try:
        proc = subprocess.run(binding.test_command, shell=True, cwd=cwd,
                              capture_output=True, text=True, timeout=_TEST_RUN_TIMEOUT)
        exit_code = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        exit_code = -1
        output = "[maestro] test run timed out after " + str(_TEST_RUN_TIMEOUT) + "s"
    payload = _record_test_run(cfg, key, tree_key=tree_key, command=binding.test_command,
                               exit_code=exit_code, output=output, actor=actor)
    return {**payload, "cached": False}


def _record_ac_check(cfg: Config, key: str, *, tree_key: str, h: str, ac_index: int, ac_text: str,
                     kind: str, command: str, exit_code: int, output: str, actor: str) -> dict:
    """Append one AcCheckCaptured event -- the per-AC counterpart to
    `_record_test_run`, same shape/idempotency rule (T-79)."""
    passed = exit_code == 0
    payload = {"tree_key": tree_key, "ac_hash": h, "ac_index": ac_index, "ac_text": ac_text,
               "kind": kind, "command": command, "exit_code": exit_code, "passed": passed}
    if not passed:
        payload["failure_excerpt"] = _bounded_excerpt(output)
    _append(cfg, key, E.AC_CHECK_CAPTURED, payload, actor=actor,
            sid=f"accheck-{key}-{tree_key}-{h}")
    return payload


def _run_shell(command: str, cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd,
                              capture_output=True, text=True, timeout=_TEST_RUN_TIMEOUT)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"[maestro] check timed out after {_TEST_RUN_TIMEOUT}s"


def _path_diff(cwd: Path, base: str, rel_path: str) -> str:
    """The branch's diff against *base*, restricted to *rel_path* -- covers
    both a tracked, modified file (anchored at the merge-base, same rule as
    `_qa_diff`, so base-advancement never leaks in) and a brand-new, not-yet
    `git add`-ed file (via `git diff --no-index`, same as `_untracked_diff`)."""
    def _git(*args):
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT)

    tracked = _git("ls-files", "--error-unmatch", "--", rel_path).returncode == 0
    if tracked:
        for ref in (f"origin/{base}", base):
            mb = _git("merge-base", ref, "HEAD")
            anchor = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else ref
            proc = _git("diff", anchor, "--", rel_path)
            if proc.returncode == 0:
                return proc.stdout
        return ""
    d = _git("diff", "--no-index", "--", "/dev/null", rel_path)
    return d.stdout if d.returncode <= 1 else ""


def _diff_added_test_names(cwd: Path, base: str, rel_path: str,
                           profile: "testlang.LanguageProfile") -> set[str]:
    """Test names newly introduced (an added line matching *profile*'s own
    `added_re`) by the branch's diff against *base*, restricted to
    *rel_path* -- what a `test:` annotation's gate actually checks presence
    against, never just "exists on disk" (that reproduces the T-55 seq-130
    false-attestation class: a green suite whose diff never actually added
    the named test). T-84: *profile* is the caller's already-resolved
    per-key language table entry (`testlang.resolve(binding.language)`),
    never a module-level regex -- the sole per-language selection point."""
    diff_text = _path_diff(cwd, base, rel_path)
    return {m.group(1) for line in diff_text.splitlines() if (m := profile.added_re.match(line))}


def _run_named_test(profile: "testlang.LanguageProfile", test_command: str, cwd: Path,
                    base: str, ann: "snap_mod.AcAnnotation") -> tuple[str, int, str]:
    """Run (or refuse to run) a `test:` annotation's check -- returns
    ``(command, exit_code, output)``, mirroring `_run_shell`'s shape so
    `run_ac_checks` can record either uniformly. *test_command* is the
    caller's already-resolved per-key command (T-83: `binding.test_command`),
    never `cfg.test_command` directly. *profile* (T-84) is the caller's
    already-resolved language table entry -- selector syntax (pytest
    `path::id`, `go test -run`, jest `-t`) comes from `profile.format_selector`,
    never hardcoded here.

    Presence in the diff is checked FIRST, before anything is ever run: a
    named test (`::<id>` form -- for python, matched by its leaf name to
    allow a class-scoped `Class::test_method`) or, for a bare file-path
    form, ANY added test in that file, must actually be part of this diff --
    a suite that happens to pass with an untouched, pre-existing test of the
    same name satisfies nothing (T-79 AC4 / the T-55 class)."""
    added = _diff_added_test_names(cwd, base, ann.path, profile)
    if ann.test_id:
        leaf = ann.test_id.rsplit("::", 1)[-1]
        command = profile.format_selector(test_command, ann.path, [ann.test_id])
        if leaf not in added:
            return command, 1, (
                f"[maestro] {ann.path}::{ann.test_id} not found among tests added by "
                f"this diff in {ann.path} -- a passing suite alone does not satisfy a "
                "test: annotation")
    else:
        if not added:
            return (f"{test_command} {ann.path}", 1,
                    f"[maestro] no test added by this diff in {ann.path} -- a passing "
                    f"suite alone does not satisfy a test: annotation")
        command = profile.format_selector(test_command, ann.path, sorted(added))
    exit_code, output = _run_shell(command, cwd)
    return command, exit_code, output


def run_ac_checks(cfg: Config, key: str, cwd: Path, *, actor: str = "dispatcher") -> dict:
    """T-79: run every ANNOTATED AC's own `test:`/`check:` check at *cwd*'s
    current tree state, and record each as an AcCheckCaptured event -- the
    per-AC counterpart to `capture_tests`'s whole-suite run.

    Called by `dispatcher._route_test_run` only once the suite itself is
    already green -- never spawns an agent session, exactly like
    `capture_tests`; both are a plain `subprocess.run` this (dispatcher)
    process makes directly. Cached per (tree_key, ac_hash), same rule as
    `capture_tests`: a tree state already checked is not re-run, just re-read.
    No-op (`{"all_passed": True, "checked": [], "summary": ""}`) when the spec
    has no acs section at all or no ACs carry an annotation.

    T-84: a `test:` annotation's added/deleted-name extraction and selector
    syntax are selected per `binding.language` (`testlang.resolve`) -- never
    hardcoded pytest here. `binding.language` is already fail-closed at
    `config.load()` time (an unrecognized value refuses to load the home at
    all -- see `config._REPO_TABLE_KEYS`'s validation), so `testlang.resolve`
    raising `UnsupportedLanguage` here should never actually happen; it is
    caught anyway (defense in depth against a binding constructed by some
    other path) and surfaced via the `"unsupported"` key instead of ever
    running (and thus ever failing-closed forever) a check that cannot mean
    anything for that language -- the caller (`_route_test_run`) turns a
    non-empty `"unsupported"` into one clear, one-time `ops.fail(...,
    dead_letter=True)` rather than a bounce back to `implementing`.
    """
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return {"all_passed": True, "checked": [], "summary": "", "unsupported": []}
    text = spec_path.read_text(encoding="utf-8")
    acs = snap_mod.parse_acs(text)
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode == "local":  # defensive -- verifying never reaches mode:local
        return {"all_passed": True, "checked": [], "summary": "", "unsupported": []}
    base = binding.base_branch
    tree_key = _tree_state_key(cwd)
    snap = snap_mod.load(cfg.home, key)
    cached = snap.ac_checks.get(tree_key, {})

    all_passed = True
    failures = []
    checked = []
    unsupported = []
    for i, t in enumerate(acs, start=1):
        ann = snap_mod.parse_ac_annotation(t)
        if ann is None:
            continue
        h = snap_mod.ac_hash(t)
        rec = cached.get(h)
        if rec is None:
            if ann.kind == "check":
                command = ann.command
                exit_code, output = _run_shell(ann.command, cwd)
            else:
                try:
                    profile = testlang.resolve(binding.language)
                except testlang.UnsupportedLanguage as exc:
                    unsupported.append({"ac_index": i, "ac_hash": h,
                                        "language": binding.language, "error": str(exc)})
                    continue
                command, exit_code, output = _run_named_test(profile, binding.test_command,
                                                              cwd, base, ann)
            rec = _record_ac_check(cfg, key, tree_key=tree_key, h=h, ac_index=i, ac_text=t,
                                   kind=ann.kind, command=command, exit_code=exit_code,
                                   output=output, actor=actor)
        checked.append({"ac_index": i, "ac_hash": h, "passed": rec["passed"]})
        if not rec["passed"]:
            all_passed = False
            failures.append(f"AC#{i} ({ann.kind}): "
                            f"{rec.get('failure_excerpt') or ('exit ' + str(rec['exit_code']))}")
    return {"all_passed": all_passed and not unsupported, "checked": checked,
            "summary": "; ".join(failures)[:2000], "unsupported": unsupported}


def record_qa_verdict(cfg: Config, key: str, ac_index: int, verdict: str, evidence: str, *,
                       axis: str = "spec", actor: str = "reconciler-qa") -> str:
    """Record an *independent* QA re-check of AC #ac_index (1-based, in spec
    order) — the counterpart to `verify_ac`'s self-attestation, meant to be
    called by a separate agent that did not write the implementation.

    `axis` (T-23) distinguishes *what* was re-checked: "spec" (default — does
    the diff satisfy this AC? the AD-4 behavior, unchanged) or "standards" (does
    the diff follow CLAUDE.md conventions + a Fowler-smell baseline? config-gated
    by `qa_standards_axis`, advisory only). The two axes fold into separate
    snapshot buckets (see snapshot.Snapshot.qa_verdicts / qa_verdicts_standards)
    and are never reranked against each other; only a "spec" fail blocks
    `set-phase awaiting-ci` (see `_refuse_if_qa_failing`).

    Content-hash keyed like `verify_ac`, but the step id also folds in the
    current `observed_seq`: unlike a self-attestation, the *same* AC is
    expected to be re-verdicted after each fix-and-retry round, so a later
    call (once the log has moved on) must record a new event rather than
    collapse into the first one.

    T-85: refuses (raises `MaestroError`, NO event appended) when the ticket's
    folded phase is not `qa`, unless `cfg.qa_phase_gate` is off (default ON) --
    the write-path counterpart to the runner-level spawn denylist
    (`dispatcher.phase_verb_denylist('implementing')`), which blocks the CLI
    verb from an `implementing`-phase runner but is a permission grant, not an
    invariant this function itself enforced until now.
    """
    if verdict not in QA_VERDICTS:
        raise store.MaestroError(f"{key}: --verdict must be one of {sorted(QA_VERDICTS)}, got {verdict!r}")
    if axis not in QA_AXES:
        raise store.MaestroError(f"{key}: --axis must be one of {sorted(QA_AXES)}, got {axis!r}")
    snap = snap_mod.load(cfg.home, key)
    if cfg.qa_phase_gate and snap.phase != Phase.QA.value:
        raise store.MaestroError(
            f"{key}: refusing qa-verdict — ticket phase is {snap.phase!r}, not 'qa'; an "
            f"independent QA verdict may only be recorded while the ticket is in the qa "
            f"phase (set qa_phase_gate = false in config.toml to disable)")
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        raise store.MaestroError(f"{key}: no spec.md to verify ACs against")
    acs = snap_mod.parse_acs(spec_path.read_text(encoding="utf-8"))
    if not (1 <= ac_index <= len(acs)):
        raise store.MaestroError(f"{key}: AC #{ac_index} out of range (spec has {len(acs)} AC(s))")
    ac_text = acs[ac_index - 1]
    h = snap_mod.ac_hash(ac_text)
    sid = step_id(key, snap.phase, snap.observed_seq, f"qaverdict-{axis}-{h}-{verdict}")
    _append(cfg, key, E.AC_QA_VERDICT,
            {"ac_hash": h, "ac_index": ac_index, "ac_text": ac_text, "verdict": verdict,
             "evidence": evidence, "axis": axis},
            actor=actor, sid=sid)
    return h


def ask(cfg: Config, key: str, text: str, *, qid: str | None = None, actor: str = "reconciler") -> str:
    qid = qid or content_hash(text)
    _append(cfg, key, E.QUESTION_ASKED, {"qid": qid, "text": text},
            actor=actor, sid=f"ask-{key}-{qid}")
    # The QuestionAsked append just above is the fold this decision is made
    # from -- reload it (cheap, in-process) so the AWAITING_HUMAN transition is
    # fenced against exactly that observed tail, not an earlier one.
    snap = snap_mod.load(cfg.home, key)
    set_phase(cfg, key, Phase.AWAITING_HUMAN, reason="asked human", actor=actor,
              expect=snap.observed_seq)
    return qid


def ask_round(cfg: Config, key: str, questions: list[tuple[str, str | None, str | None]], *,
             actor: str = "reconciler") -> list[str]:
    """Ask the whole settled frontier in one round: N questions, numbered, each
    optionally carrying a recommended answer -- one dispatcher wake and one
    human round-trip instead of N, since a reconciler holding a settled
    question back to ask it alone next round is the most expensive schedule
    available here (a dispatcher wake + an hours-long human round-trip +
    a full reconciler spawn, paid once per question instead of once per round).

    Each item is `(text, recommend, qid)`; `recommend`/`qid` may be None to
    auto-derive (qid defaults to `content_hash(text)`, same as plain `ask`) --
    an explicit qid is only needed for a question a later reconcile step
    routes on by qid prefix (e.g. `research-approval-<key>`).

    `open_questions` is already a qid-keyed dict (`ask` above), so this needs
    no event-shape change: one QuestionAsked per question, numbered in the
    human-facing text, with the recommendation folded into that same text
    field -- every existing open_questions reader (notify, projection, tui,
    context) already treats it as an opaque display string. One phase
    transition for the whole round, not one per question.
    """
    if not questions:
        raise store.MaestroError(f"{key}: ask_round needs at least one question")
    qids = []
    total = len(questions)
    for i, (text, recommend, qid) in enumerate(questions, start=1):
        qid = qid or content_hash(text)
        numbered = f"{i}/{total}. {text}" if total > 1 else text
        if recommend:
            numbered += f"\n   Recommended: {recommend}"
        _append(cfg, key, E.QUESTION_ASKED, {"qid": qid, "text": numbered},
                actor=actor, sid=f"ask-{key}-{qid}")
        qids.append(qid)
    # Same fencing rationale as `ask` above: reload after the last QuestionAsked
    # append and fence the phase transition against that observed tail.
    snap = snap_mod.load(cfg.home, key)
    set_phase(cfg, key, Phase.AWAITING_HUMAN,
              reason=f"asked human ({total} question(s) this round)", actor=actor,
              expect=snap.observed_seq)
    return qids


_ROUND_PREFIX_RE = re.compile(r"^(\d+)/(\d+)\. ")
_RECOMMEND_SEP = "\n   Recommended: "


def parse_round_question(text: str) -> tuple[int | None, int | None, str, str | None]:
    """Split one `open_questions` display string -- built by `ask`/`ask_round` above
    -- back into `(position, total, body, recommend)`. `position`/`total` are None
    for a plain single question (`ask`, or a one-question round); `recommend` is
    None when the question carries no recommendation. Never raises: a string that
    doesn't match the round format round-trips as `(None, None, text, None)`, so
    every existing reader that already treats `open_questions` values as opaque
    display strings is unaffected -- this is purely additive, for a reader (the
    TUI) that wants the structured pieces back out."""
    position = total = None
    m = _ROUND_PREFIX_RE.match(text)
    if m:
        position, total = int(m.group(1)), int(m.group(2))
        text = text[m.end():]
    body, sep, recommend = text.partition(_RECOMMEND_SEP)
    return position, total, body, (recommend if sep else None)


def route_conflict(cfg: Config, key: str, pr_number: int, *, actor: str = "reconciler") -> bool:
    """Route a CONFLICTING PR back into `implementing` so the agent rebases onto
    the base branch, resolves the conflicts, and pushes — auto-resolution that
    actually updates the PR. Idempotent: a no-op if already implementing. Returns
    True if it moved the ticket. The agent escalates to a human (plain `maestro
    ask`) only when it cannot resolve the conflict itself."""
    snap = snap_mod.load(cfg.home, key)
    if snap.phase == Phase.IMPLEMENTING.value:
        return False
    set_phase(cfg, key, Phase.IMPLEMENTING,
              reason=f"resolve merge conflict for PR #{pr_number}", actor=actor,
              expect=snap.observed_seq)
    return True


def route_stale(cfg: Config, key: str, *, base_branch: str = "main",
                policy: str = "always", actor: str = "dispatcher") -> bool:
    """Route a ticket whose worktree has drifted behind its repo's base branch
    back into `implementing` so the reconciler rebases (idempotent — a no-op if
    already implementing). Mirrors `route_conflict`'s auto-resolution path, but
    fires from the dispatcher's proactive drift check
    (`dispatcher.sync_worktrees`) rather than a GitHub-reported CONFLICTING PR.
    `base_branch` names the ticket's actual resolved repo binding (`main` for
    an unbound ticket / single-repo board). `policy` (MTO-2: the
    `base_drift_policy` that decided this call should fire at all — the
    caller, `dispatcher.sync_worktrees`, already gated on it) is recorded in
    the reason so the event log explains *why* a given re-route was allowed.
    Returns True if it moved the ticket."""
    snap = snap_mod.load(cfg.home, key)
    if snap.phase == Phase.IMPLEMENTING.value:
        return False
    set_phase(cfg, key, Phase.IMPLEMENTING,
              reason=(f"origin/{base_branch} advanced (policy={policy}) — "
                      f"rebase worktree onto latest {base_branch}"),
              actor=actor, expect=snap.observed_seq)
    return True


def ask_conflict(cfg: Config, key: str, pr_number: int, *, actor: str = "reconciler") -> bool:
    """Escalate an unresolvable PR merge conflict to the human (idempotent — skips
    if already open). Used only when the agent's own rebase in `implementing`
    cannot resolve the conflict; the normal path is `route_conflict`."""
    snap = snap_mod.load(cfg.home, key)
    qid = f"conflict-{key}-{pr_number}"
    if qid in snap.open_questions:
        return False
    text = (f"PR #{pr_number} has a merge conflict the agent could not auto-resolve. "
            f"Please rebase, resolve the conflicts, and push — or answer with guidance.")
    ask(cfg, key, text, qid=qid, actor=actor)
    return True


def check_merged(cfg: Config, key: str, pr_state: str, *, actor: str = "reconciler") -> bool:
    """Finalize if the PR is merged — callable from any phase (idempotent).

    Records PrUpdated(merged=True) then Finalized. Returns True if finalized,
    False if the state isn't MERGED or the ticket is already done.
    """
    if pr_state.upper() != "MERGED":
        return False
    snap = snap_mod.load(cfg.home, key)
    if snap.phase == Phase.DONE.value:
        return False
    _append(cfg, key, E.PR_UPDATED, {"merged": True},
            actor=actor, sid=f"pr-merged-{key}")
    finalize(cfg, key, actor=actor)
    return True


# UX-1: same loose-frontmatter grammar `gates.parse_spec_overrides` scans
# (stop at the first `##` header) -- kept as its own copy here, matching
# `health._spec_runner_fields`'s precedent, rather than importing a private
# name out of `gates`.
_FRONTMATTER_FIELD_RE = re.compile(r"^([a-zA-Z_]\w*)\s*:\s*(.+)$")


def _set_frontmatter_fields(spec_text: str, fields: dict) -> str:
    """Insert or replace each ``key: value`` line in *spec_text*'s loose
    frontmatter block (everything before the first ``##`` header), preserving
    every other byte untouched -- an existing field's line is rewritten in
    place (same position, same trailing newline style); a field not already
    present is inserted right before the first ``##`` header (or at the end
    of the file, if there is none). Never touches a line at or past the first
    ``##`` header, so a body mention of e.g. ``runner: foo`` in prose is
    immune."""
    lines = spec_text.splitlines(keepends=True)
    remaining = dict(fields)
    out: list[str] = []
    insert_at = None
    for line in lines:
        stripped = line.rstrip("\n")
        if insert_at is None and stripped.startswith("##"):
            insert_at = len(out)
        m = _FRONTMATTER_FIELD_RE.match(stripped) if insert_at is None else None
        if m and m.group(1) in remaining:
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{m.group(1)}: {remaining.pop(m.group(1))}{newline}")
        else:
            out.append(line)
    if insert_at is None:
        insert_at = len(out)
    if remaining:
        out[insert_at:insert_at] = [f"{k}: {v}\n" for k, v in remaining.items()]
    return "".join(out)


def set_runner(cfg: Config, key: str, *, runner: str | None = None,
               runner_model: str | None = None) -> dict:
    """[human] Rewrite *key*'s spec `runner:`/`runner_model:` front-matter
    line(s) surgically (`_set_frontmatter_fields`) -- the write-through this
    ticket's `gates.runner_editable`/`ops.schedule_*`'s `_validate_cadence`
    precedent both point at, so the CLI and (later) the TUI share one
    validating function.

    Raises `store.MaestroError` -- never a bare exception -- if neither field
    is given, no spec.md exists, or `gates.runner_editable` says *key* has
    already moved past the point a runner choice can still take effect (a
    running session's spawn args were frozen at launch; a late edit would
    silently apply only to the *next* session, which is worse than
    refusing). Appends NO event: the spec hash on disk no longer matching the
    snapshot's own recorded hash is what wakes the ticket next sweep
    (`dispatcher.is_due`'s "spec-changed" branch) -- the same mechanism any
    other direct human spec edit already relies on.

    Never gates on the model being locally installed/reachable -- WARNs only,
    the same three-valued verdict `health.check_ollama_models`
    (`health.check_pi_models` for a pi `runner`) computes, just run here so
    the human sees it immediately rather than waiting for the next `maestro
    doctor`. T-61: the catalogue is sourced from the runner's OWN provider
    module -- opencode from `providers/ollama.py`, pi from `providers/pi.py`
    (needs `cfg.home` to resolve `store.pi_agent_dir`) -- never ollama's for
    a pi `runner_model`. Returns
    ``{"runner", "runner_model", "warning": str | None}``.
    """
    if runner is None and runner_model is None:
        raise store.MaestroError(
            f"runner {key}: at least one of --runner/--runner-model is required")
    spec_file = store.spec_path(cfg.home, key)
    if not spec_file.exists():
        raise store.MaestroError(f"{key}: no spec.md to set runner on")
    snap = snap_mod.load(cfg.home, key)
    if not gates.runner_editable(cfg.home, key, snap):
        raise store.MaestroError(
            f"runner {key}: no longer editable -- a reconciler session for this "
            "key is currently in flight, and its spawn args (runner included) "
            "were already frozen at launch; wait for it to exit and try again")

    updates = {f: v for f, v in (("runner", runner), ("runner_model", runner_model))
              if v is not None}
    text = spec_file.read_text(encoding="utf-8")
    store.atomic_write(spec_file, _set_frontmatter_fields(text, updates))

    warning = None
    if runner and runner != "claude" and runner_model:
        if runner == "pi":
            from .providers import pi as pi_mod
            models, reason = pi_mod.fetch_models(store.pi_agent_dir(cfg.home))
            verdict, vreason = pi_mod.verdict_for_model(models, reason, runner_model)
            source = "pi"
            suggestions = pi_mod.model_names(models) if models else []
        else:
            from .providers import ollama as ollama_mod
            models, reason = ollama_mod.fetch_models()
            verdict, vreason = ollama_mod.verdict_for_model(models, reason, runner_model)
            source = "ollama daemon"
            suggestions = ollama_mod.model_names(models, tool_capable_only=True) if models else []
        if verdict == "unreachable":
            warning = f"{source} unreachable ({vreason}) -- runner_model not validated"
        elif verdict == "missing":
            warning = f"{vreason}; available models: {suggestions or '(none)'}"
    return {"runner": runner, "runner_model": runner_model, "warning": warning}


def observe_spec(cfg: Config, key: str, *, actor: str = "reconciler") -> str | None:
    h = spec_hash_on_disk(cfg.home, key)
    if h is None:
        return None
    _append(cfg, key, E.SPEC_OBSERVED, {"spec_hash": h}, actor=actor, sid=f"spec-{key}-{h}")
    return h


def requeue(cfg: Config, key: str, seconds: int, *, actor: str = "reconciler") -> None:
    at = store.now_epoch() + max(0, seconds)
    snap = snap_mod.load(cfg.home, key)
    _append(cfg, key, E.REQUEUE_SCHEDULED, {"at": at}, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, f"requeue:{int(at)}"))


def checked(cfg: Config, key: str, *, actor: str = "reconciler") -> dict | None:
    """RB-10: record a genuine no-op -- this reconcile step ran to completion
    and correctly decided nothing was due.

    `_allow_spawn`'s no-progress watchdog (dispatcher.py) infers "crashed
    before appending a single event" from "observed_seq did not advance". A
    reconciler that runs to completion and correctly finds nothing to do also
    appends nothing, so the two opposite outcomes had an identical signature
    and the watchdog eventually failed the healthy one (the 2026-08-14 T-55
    incident, RB-10's Notes). Call this immediately before `maestro release`
    on a genuine no-op exit path so `observed_seq` advances by exactly one and
    the existing seq-comparison in `_allow_spawn` treats the sweep as
    progress -- no new watchdog mechanism, just closing the gap in the
    signal it already reads.

    Step id keyed on (key, phase, observed_seq), the same shape `requeue`/
    `record_impl_turn`/`fail` use: two attempts that both read the log at the
    same not-yet-advanced seq (a lost race between concurrent callers, or a
    retried write whose first attempt actually landed) collapse to one event
    instead of two -- but a call that runs *after* a prior `checked` already
    landed reads the advanced seq and legitimately appends a fresh one; that
    is not a duplicate, it is a second real check-in, exactly like a second
    `impl-turn` call is a second real turn. A session killed by `run_watchdog`
    (a wedged process, reaped by pgid) never reaches this call at all, so a
    kill cannot counterfeit a completed no-op; it still falls through to the
    unmodified crash path.

    Runner-agnostic by construction, not by branching on `runner`: this is a
    `maestro` CLI verb the reconcile *skill script* calls, and the identical
    script runs unmodified under `ClaudeCliSessions` and `OpencodeCliSessions`
    alike (see claims' pid+start_epoch keying for the same shape of
    argument). There is exactly one code path to this event, for both.

    Cost (quiet-path log growth, per spec AC): the payload is `{}` -- the
    envelope (type/actor/step_id/ts/seq) is the whole cost, on the order of
    150-200 bytes per event on disk. A ticket spawned at the anti-thrash floor
    (`spawn_floor_s`, 300s default) that no-ops every single spawn appends at
    most 288 of these a day, ~50KB/day, until a human unblocks it or a
    sleeping-phase fix (e.g. T-65 for `degraded`) stops it from being
    respawned at all. `Checked` carries no QA/AC/PR state, so it costs
    `fold`/`compact`/`archive` nothing beyond that same linear read+write --
    unlike `Note`, nothing downstream branches on it.
    """
    snap = snap_mod.load(cfg.home, key)
    return _append(cfg, key, E.CHECKED, {}, actor=actor,
                   sid=step_id(key, snap.phase, snap.observed_seq, "checked"))


def record_impl_turn(cfg: Config, key: str, *, role: str = "implementer",
                      actor: str = "reconciler") -> dict:
    """Append one ``ImplTurnRecorded{turn, role}``, folding into ``snapshot.impl_turns``.

    Crossing ``cfg.max_impl_turns`` parks the ticket via `fail` (the same backoff/
    dead-letter machinery `max_spawn_attempts`'s watchdog uses) instead of letting
    it keep churning edit/test cycles -- so a non-converging implementing session
    stops on its own. The ceiling check reads the just-folded snapshot value, not
    a counter held in the calling session, so it is exact under crash-and-respawn
    (a respawned session sees the same folded count a prior one left behind).
    """
    snap = snap_mod.load(cfg.home, key)
    turn = snap.impl_turns + 1
    _append(cfg, key, E.IMPL_TURN, {"turn": turn, "role": role}, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, f"implturn:{turn}"))
    snap = snap_mod.load(cfg.home, key)
    parked = False
    if cfg.max_impl_turns and snap.impl_turns >= cfg.max_impl_turns:
        fail(cfg, key,
             f"max_impl_turns ceiling reached ({snap.impl_turns}/{cfg.max_impl_turns})",
             actor=actor)
        parked = True
    return {"turn": snap.impl_turns, "parked": parked}


def fail(cfg: Config, key: str, error: str, *, actor: str = "reconciler",
         dead_letter: bool = False, kind: str | None = None,
         provider_state: str | None = None) -> str:
    """Record a failure; back off, or dead-letter if over the threshold.

    ``dead_letter=True`` (T-45) skips the failure-count threshold and
    dead-letters on THIS call, for a structural failure a sweep-later retry
    cannot possibly fix (e.g. a resolved reconcile command that doesn't exist
    in the session's cwd) -- waiting out `max_failures` backed-off retries
    first would just reproduce the respawn-forever bug this exists to stop.

    ``kind`` (RB-11) tags the ``Failed``/``Stalled`` payload for a caller that
    knows THIS failure is a burn-detector park (``burn.should_park``), not a
    generic error -- folded into ``Snapshot.burning`` so the human-facing
    surfaces (``maestro status``, ``NEEDS-YOU.md``) can tell "parked, waiting
    for you" apart from "burning" (spec AC5). Omitted (``None``) for every
    other caller; never itself changes whether this call dead-letters.

    ``provider_state`` (T-89, AC5) tags the payload with ``kind: "provider"``
    plus the observed ``health.check_provider_availability`` state ("erroring"
    / "no_network") for a caller that knows the fleet's provider check was
    non-ok when this failure fired -- e.g. the watchdog reaping a wedged
    session during an outage, so it reads as provider-caused rather than a
    bare timeout (folded into ``Snapshot.last_error_kind``/
    ``last_error_state``, rendered in the TUI detail pane). Mutually
    exclusive with ``kind`` in practice (a burn park and a provider outage are
    different failure classes); if both are given, ``provider_state`` wins --
    it is the more specific, evidence-backed marker.
    """
    snap = snap_mod.load(cfg.home, key)
    marker = ({"kind": "provider", "state": provider_state} if provider_state
              else ({"kind": kind} if kind else {}))
    failed_payload = {"error": error, **marker}
    _append(cfg, key, E.FAILED, failed_payload, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, "fail"))
    snap = snap_mod.load(cfg.home, key)
    if dead_letter or snap.failure_count >= cfg.max_failures:
        stalled_payload = {"reason": f"{snap.failure_count} failures: {error}", **marker}
        _append(cfg, key, E.STALLED, stalled_payload,
                actor=actor, sid=f"deadletter-{key}-{snap.observed_seq}")
        _write_deadletter(cfg, key, error)
        return "dead-letter"
    delay = min(cfg.backoff_cap, cfg.backoff_base * (2 ** snap.failure_count))
    delay = int(delay * random.uniform(0.7, 1.3))  # jitter: avoid thundering herd
    requeue(cfg, key, delay, actor=actor)
    return f"backoff:{delay}s"


def _write_deadletter(cfg: Config, key: str, error: str) -> None:
    tail = event_log.read(cfg.home, key)[-8:]
    body = [f"# {key} — dead-lettered", "", f"Last error: {error}", "",
            "## Recent events"]
    for e in tail:
        body.append(f"- seq {e['seq']} {e['type']} ({e['ts']})")
    body += ["", "Revive with: `maestro cmd %s retry`  ·  Drop with: `maestro cmd %s discard`"
             % (key, key)]
    store.atomic_write(store.deadletter_path(cfg.home, key), "\n".join(body) + "\n")


def fold_inbox(cfg: Config, key: str) -> list[dict]:
    """Fold pending human commands into events (idempotent), WITHOUT acking.
    The reconciler acks only after it has advanced the phase, so a crash mid-step
    re-reads the same commands next sweep.
    """
    base = len(store.read_jsonl(store.inbox_path(cfg.home, key))) - len(inbox.pending(cfg.home, key))
    pend = inbox.pending(cfg.home, key)
    snap = snap_mod.load(cfg.home, key)
    open_qids = list(snap.open_questions.keys())
    for i, cmd in enumerate(pend):
        idx = base + i
        command = cmd.get("command", "")
        _append(cfg, key, E.COMMAND_RECEIVED, {"command": command, "args": cmd.get("args", {})},
                actor="human", sid=f"cmd-{key}-{idx}")
        if command in ANSWER_COMMANDS:
            target = cmd.get("args", {}).get("qid")
            qids = [target] if target else open_qids
            answer = cmd.get("args", {}).get("text", command)
            for qid in qids:
                _append(cfg, key, E.QUESTION_ANSWERED, {"qid": qid, "answer": answer},
                        actor="human", sid=f"ans-{key}-{idx}-{qid}")
    return pend


def finalize(cfg: Config, key: str, *, actor: str = "reconciler") -> None:
    _append(cfg, key, E.FINALIZED, {}, actor=actor, sid=f"finalize-{key}")


def _prune_plan(cfg: Config, key: str, *, now: float | None = None) -> list[dict]:
    """Compute which of *key*'s session log files retention settings would delete,
    without touching disk. 0/None on either knob means that dimension is unlimited;
    both unlimited is a no-op. Never plans to delete the log belonging to a
    currently-live, correctly-identified reconciler (pid alive AND not a
    verified-denied identity — a reused pid is definitionally not our
    reconciler, so its stale log is fair game)."""
    from . import claims
    from .sessions import list_sessions

    retention_days = cfg.session_log_retention_days
    max_per_ticket = cfg.session_log_max_per_ticket
    if not retention_days and not max_per_ticket:
        return []

    all_files = list_sessions(cfg.home, key)
    if not all_files:
        return []

    # Paths belonging to the live session (if any) are off-limits.
    live_paths: set[str] = set()
    claim = claims.read_claim(cfg.home, key)
    if (claim and claims.pid_alive(claim.get("pid"))
            and claims.verify_claim(cfg.home, key) != "denied"):
        lp = claim.get("log_path")
        if lp:
            live_paths.add(lp)
            # RF-3 / T-58: strip whichever of the four known suffixes lp actually
            # has, then reconstruct all four siblings -- a live session is exactly
            # one file, but exclude-by-stem regardless of which format it happens
            # to be logged in.
            stem = (lp.removesuffix(".stream.jsonl")
                      .removesuffix(".opencode.jsonl")
                      .removesuffix(".pi.jsonl")
                      .removesuffix(".log"))
            live_paths.add(stem + ".log")
            live_paths.add(stem + ".stream.jsonl")
            live_paths.add(stem + ".opencode.jsonl")
            live_paths.add(stem + ".pi.jsonl")

    # Group files by session_id so .log and .stream.jsonl for the same session
    # are treated as one unit for the "max" limit.
    by_id: dict[str, list[dict]] = {}
    for f in all_files:
        by_id.setdefault(f["session_id"], []).append(f)

    # Sort session ids: newest epoch first.
    sorted_ids = sorted(
        by_id,
        key=lambda sid: max(f["epoch"] for f in by_id[sid]),
        reverse=True,
    )

    to_delete: set[str] = set()
    now = now if now is not None else store.now_epoch()

    if max_per_ticket:
        kept = 0
        for sid in sorted_ids:
            files = by_id[sid]
            if any(f["path"] in live_paths for f in files):
                continue  # live sessions don't count toward the limit
            if kept < max_per_ticket:
                kept += 1
            else:
                for f in files:
                    to_delete.add(f["path"])

    if retention_days:
        cutoff = now - retention_days * 86400
        for sid in sorted_ids:
            files = by_id[sid]
            if any(f["path"] in live_paths for f in files):
                continue
            if max(f["epoch"] for f in files) < cutoff:
                for f in files:
                    to_delete.add(f["path"])

    return [f for f in all_files if f["path"] in to_delete]


def prune_session_logs(cfg: Config, key: str, *, now: float | None = None) -> tuple[int, int]:
    """Delete stale session log files for *key* per retention settings.

    Never removes the log belonging to a currently-live reconciler (pid alive).
    Returns (files deleted, bytes reclaimed).
    """
    pruned = 0
    pruned_bytes = 0
    for f in _prune_plan(cfg, key, now=now):
        path = Path(f["path"])
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        pruned += 1
        pruned_bytes += size
    return pruned, pruned_bytes


def prune_session_logs_dry_run(cfg: Config, key: str, *, now: float | None = None) -> tuple[int, int]:
    """Like ``prune_session_logs`` but only reports what would be deleted."""
    total_bytes = 0
    plan = _prune_plan(cfg, key, now=now)
    for f in plan:
        try:
            total_bytes += Path(f["path"]).stat().st_size
        except OSError:
            pass
    return len(plan), total_bytes


def prune_all_session_logs(cfg: Config, *, now: float | None = None,
                            dry_run: bool = False, keys: list[str] | None = None) -> dict:
    """Prune session logs for every key reachable under ``agent-logs/`` (or just
    *keys*, when given, for a targeted single-key sweep).

    Walks ``agent-logs/*`` directly rather than ``dispatcher.list_keys`` so orphaned
    log dirs (no ticket/events/snapshot) stay reachable, while launchd's live
    ``dispatch.out.log`` / ``dispatch.err.log`` are skipped (not directories) and any
    non-key-shaped name is skipped too (belt and braces alongside the directory
    check). A per-key ``OSError`` (e.g. an unreadable directory) is swallowed so one
    bad key never stops the rest of the walk.
    """
    home = cfg.home
    if keys is not None:
        candidates = list(keys)
    else:
        candidates = []
        log_root = home / "agent-logs"
        if log_root.exists():
            for entry in sorted(log_root.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    store.validate_key(entry.name)
                except store.MaestroError:
                    continue
                candidates.append(entry.name)

    per_key: dict[str, dict] = {}
    errors: dict[str, str] = {}
    total_files = 0
    total_bytes = 0
    for key in candidates:
        try:
            if dry_run:
                count, nbytes = prune_session_logs_dry_run(cfg, key, now=now)
            else:
                count, nbytes = prune_session_logs(cfg, key, now=now)
        except OSError as e:
            errors[key] = str(e)
            continue
        if count:
            per_key[key] = {"pruned_logs": count, "pruned_bytes": nbytes}
        total_files += count
        total_bytes += nbytes
    return {"per_key": per_key, "pruned_logs": total_files, "pruned_bytes": total_bytes,
            "errors": errors}


def compact(cfg: Config, key: str) -> dict:
    """Move events older than the snapshot into the archive.

    Under the active-log lock:
    - Reads snapshot to learn observed_seq (the high-water mark of the last fold).
    - Events with seq <= observed_seq are "pre-snapshot" and move to the archive.
    - Events with seq > observed_seq stay in the active log.
    - Archive grows monotonically; step_ids from archived events are still visible
      to _scan_tail so idempotency is preserved across compactions.
    """
    snap = snap_mod.load(cfg.home, key)
    cutoff = snap.observed_seq
    active_path = store.events_path(cfg.home, key)
    archive_path = store.events_archive_path(cfg.home, key)

    with store.file_lock(active_path):
        active_events = store.read_jsonl(active_path)
        pre = [e for e in active_events if isinstance(e.get("seq"), int) and e["seq"] <= cutoff]
        post = [e for e in active_events if not (isinstance(e.get("seq"), int) and e["seq"] <= cutoff)]

        if not pre:
            return {"archived": 0, "remaining": len(post), "cutoff_seq": cutoff}

        # Avoid double-archiving: only append events with seq > last archived seq.
        archived_events = store.read_jsonl(archive_path)
        last_archived_seq = max((e["seq"] for e in archived_events if isinstance(e.get("seq"), int)), default=0)
        to_archive = [e for e in pre if e["seq"] > last_archived_seq]

        # Durability ordering is what makes this move crash-safe: the archive append
        # must be flushed + fsynced to disk BEFORE the active log is replaced. If a
        # crash lands after this line but before the active log is rewritten below,
        # the to_archive events are simply duplicated (present in both files, still
        # fully readable) rather than lost -- event_log.read dedups by seq, so that
        # window folds to the same snapshot as before the compaction. Reusing
        # store.append_line (rather than hand-rolling the write) is what gives us
        # that flush+fsync for free; it's called once with all lines joined so the
        # whole batch is one durable append.
        if to_archive:
            lines = "\n".join(json.dumps(ev, separators=(",", ":")) for ev in to_archive)
            store.append_line(archive_path, lines)

        # Rewrite active log with only post-snapshot events, through the same
        # fsync-then-replace-then-fsync-dir sequence as every other durable write in
        # the package (store.atomic_write) -- a bare tmp.replace() left the
        # replacement in the page cache, so a crash right after could lose it. This
        # also picks up atomic_write's per-call mkstemp-derived tmp name (RB-5), so
        # two concurrent compactions of the same key (impossible today under
        # file_lock, but a future caller) can't collide on a fixed ".compact.tmp"
        # name.
        data = "".join(json.dumps(ev, separators=(",", ":")) + "\n" for ev in post)
        store.atomic_write(active_path, data)

    pruned_logs, pruned_bytes = prune_session_logs(cfg, key)
    return {"archived": len(to_archive), "remaining": len(post), "cutoff_seq": cutoff,
            "pruned_logs": pruned_logs, "pruned_bytes": pruned_bytes}


def _archive_key_files(home: Path, key: str) -> None:
    """Relocate every home-scanned artifact of *key* out of the active tree.

    Moving events + the snapshot (not just the ticket dir) is what makes
    ``dispatcher.list_keys`` stop sweeping the key -- it globs those two
    directories directly. ``snapshot.load`` falls back to the archived
    snapshot path, so a ``dependsOn`` on an archived-done ticket still
    resolves correctly instead of blocking forever on a phantom fresh snapshot.
    """
    pairs = [
        (store.ticket_dir(home, key), home / "tickets" / "_archive" / key),
        (store.events_path(home, key), store.archived_events_path(home, key)),
        (store.events_archive_path(home, key), store.archived_events_archive_path(home, key)),
        (store.snapshot_path(home, key), store.archived_snapshot_path(home, key)),
    ]
    for src, dst in pairs:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)


def archive_done(cfg: Config, *, after: float | None = None, now: float | None = None) -> list[str]:
    """Move DONE tickets out of the active scan into ``_archive`` locations.

    ``after`` (seconds) is a grace period since the ticket's last event
    (``snapshot.updated_ts``) -- a freshly-DONE ticket stays visible for that
    long before disappearing from dashboards/``list_keys``. ``None``/0 archives
    on the very next call.
    """
    from .dispatcher import list_keys
    if now is None:
        now = store.now_epoch()
    moved = []
    for key in list_keys(cfg.home):
        snap = snap_mod.load(cfg.home, key)
        if snap.phase != Phase.DONE.value:
            continue
        if after:
            if not snap.updated_ts:
                continue
            try:
                done_epoch = datetime.fromisoformat(snap.updated_ts).timestamp()
            except ValueError:
                continue
            if now - done_epoch < after:
                continue
        _archive_key_files(cfg.home, key)
        moved.append(key)
    return moved


# --- schedule (GA-13): the single path to config.write_scheduled ------------
# Board-wide, not per-ticket-key: these mutate `[[scheduled]]` tasks in
# config.toml rather than a ticket's event log. Both the CLI `schedule` verbs
# and the TUI's ScheduleScreen bindings (add/edit/toggle) call these -- neither
# surface loads/mutates/writes `cfg.scheduled` itself anymore, so the
# duplicate-name and task-not-found handling can't drift between them.

def _find_scheduled(tasks: list[dict], name: str) -> int:
    for i, t in enumerate(tasks):
        if t.get("name") == name:
            return i
    return -1


def _validate_cadence(task: dict, verb: str) -> None:
    """GA-19: exactly one of `every`/`cron` -- never both, never neither -- and
    whichever is set must actually parse; a set `tz` must resolve to a real IANA
    zone. Shared by `schedule_add`/`schedule_edit` so the CLI and the TUI's
    `_ScheduleModal` (which enforces the same rule client-side, for immediate
    feedback) can never drift apart on what "valid cadence" means. Raises
    `store.MaestroError` with an actionable message; never writes on failure --
    callers only reach `write_scheduled` after this returns cleanly.
    """
    every = task.get("every")
    cron = task.get("cron")
    if bool(every) == bool(cron):
        raise store.MaestroError(
            f"schedule {verb}: exactly one of 'every' or 'cron' is required")
    if every:
        try:
            schedule_mod.parse_every(every)
        except ValueError as e:
            raise store.MaestroError(f"schedule {verb}: {e}") from e
    else:
        try:
            schedule_mod.parse_cron(cron)
        except ValueError as e:
            raise store.MaestroError(f"schedule {verb}: {e}") from e
    if task.get("tz"):
        try:
            schedule_mod.resolve_tz(task["tz"])
        except ValueError as e:
            raise store.MaestroError(f"schedule {verb}: {e}") from e


def schedule_add(cfg: Config, task: dict) -> dict:
    """Append a new `[[scheduled]]` task and rewrite config.toml.

    Owns the validation both surfaces need: `name`/`prompt` are required,
    exactly one of `every`/`cron` must be set and parse (`_validate_cadence`),
    and `name` must not already be taken. Raises `store.MaestroError` (never a
    bare exception) on any of those, so the CLI and TUI can both catch one
    error type and surface its message as-is.
    """
    name = (task.get("name") or "").strip()
    if not name:
        raise store.MaestroError("schedule add: 'name' is required")
    if not task.get("prompt"):
        raise store.MaestroError("schedule add: 'prompt' is required")
    _validate_cadence(task, "add")
    tasks = list(cfg.scheduled)
    if _find_scheduled(tasks, name) >= 0:
        raise store.MaestroError(f"schedule add: a task named {name!r} already exists")
    new_task = {**task, "name": name}
    tasks.append(new_task)
    config_mod.write_scheduled(cfg.home, tasks)
    return new_task


def schedule_edit(cfg: Config, name: str, updates: dict) -> dict:
    """Merge `updates` onto the task currently named `name` and rewrite
    config.toml. A changed `name` in `updates` renames the task (the TUI's edit
    modal always submits the full field set, `name` included, so this is what
    makes an in-place rename through it safe). Raises `store.MaestroError` if
    no task is named `name`, the merged cadence fails `_validate_cadence`, or
    the merged name is blank or already taken by a different task.
    """
    tasks = list(cfg.scheduled)
    idx = _find_scheduled(tasks, name)
    if idx < 0:
        raise store.MaestroError(f"schedule edit: no task named {name!r}")
    merged = {**tasks[idx], **updates}
    new_name = (merged.get("name") or "").strip()
    if not new_name:
        raise store.MaestroError("schedule edit: 'name' cannot be blank")
    _validate_cadence(merged, "edit")
    for i, t in enumerate(tasks):
        if i != idx and t.get("name") == new_name:
            raise store.MaestroError(f"schedule edit: a task named {new_name!r} already exists")
    merged["name"] = new_name
    tasks[idx] = merged
    config_mod.write_scheduled(cfg.home, tasks)
    return merged


def schedule_remove(cfg: Config, name: str) -> dict:
    """Remove the task named `name` and rewrite config.toml. Raises
    `store.MaestroError` if no task is named `name`."""
    tasks = list(cfg.scheduled)
    idx = _find_scheduled(tasks, name)
    if idx < 0:
        raise store.MaestroError(f"schedule rm: no task named {name!r}")
    removed = tasks.pop(idx)
    config_mod.write_scheduled(cfg.home, tasks)
    return removed


def schedule_set_enabled(cfg: Config, name: str, enabled: bool) -> dict:
    """Set `enabled` on the task named `name` in place and rewrite
    config.toml. Raises `store.MaestroError` if no task is named `name`."""
    tasks = list(cfg.scheduled)
    idx = _find_scheduled(tasks, name)
    if idx < 0:
        verb = "enable" if enabled else "disable"
        raise store.MaestroError(f"schedule {verb}: no task named {name!r}")
    tasks[idx] = {**tasks[idx], "enabled": enabled}
    config_mod.write_scheduled(cfg.home, tasks)
    return tasks[idx]
