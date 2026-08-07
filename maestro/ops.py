"""High-level reconciler operations — the verbs an agent uses, each correct by
construction. Every verb: appends event(s) with a deterministic step-id, then
refreshes the snapshot. Idempotent under crash-and-respawn.
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from . import backup
from . import events as E
from . import context as context_mod
from . import event_log, inbox, repos as repos_mod, snapshot as snap_mod, store
from .config import Config
from .dispatcher import spec_hash_on_disk
from .idempotency import content_hash, step_id
from .statemachine import Phase, can_transition

ANSWER_COMMANDS = {"ans", "answer", "approve", "yes", "ok", "no", "reject", "discard", "retry"}


def _append(cfg: Config, key: str, type: str, payload: dict, *, actor: str,
            sid: str | None, expect: int | None = None) -> dict | None:
    ev = event_log.append(cfg.home, key, type, payload, actor=actor,
                          step_id=sid, expected_last_seq=expect)
    snap_mod.rebuild(cfg.home, key)
    context_mod.regenerate(cfg.home, key)
    return ev


def set_phase(cfg: Config, key: str, phase: Phase, *, reason: str = "", actor: str = "reconciler",
              requeue_in: int | None = None, expect: int | None = None, force: bool = False) -> dict | None:
    """Advance the ticket's phase.

    Entering `awaiting-ci` is gated two ways: a ticket with unattested ACs
    refuses (raises `MaestroError`, non-zero exit, NO event appended) unless
    `force=True`, and one with a failing independent QA verdict on a current AC
    refuses unconditionally (`force` does not override QA — that gate is fixed
    by re-running `maestro qa-verdict`, not by a human override). The `--force`
    escape hatch is for a human overriding the unverified-ACs gate; the event
    log still has to show that they did, so a forced transition records
    `forced_by=<actor>` on the PhaseChanged event plus a Note spelling out the
    count.
    """
    snap = snap_mod.load(cfg.home, key)
    unverified = _acs_unverified_count(cfg, key, snap) if phase == Phase.AWAITING_CI else 0
    if unverified > 0 and not force:
        raise store.MaestroError(
            f"{key}: refusing awaiting-ci — {unverified} acceptance criteria unverified; "
            f"run `maestro verify-ac` for each, or pass --force to override")
    forced = unverified > 0 and force
    if phase == Phase.AWAITING_CI:
        _refuse_if_qa_failing(cfg, key, snap)

    src = Phase(snap.phase)
    if src != phase and not can_transition(src, phase):
        # Not fatal — log it, but the engine trusts the agent's judgment.
        _append(cfg, key, E.NOTE, {"text": f"unusual transition {src.value}->{phase.value}"},
                actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, f"note-transition-{phase.value}"))
        snap = snap_mod.load(cfg.home, key)

    payload = {"phase": phase.value, "reason": reason}
    if forced:
        payload["forced_by"] = actor
    sid = step_id(key, snap.phase, snap.observed_seq, f"phase:{phase.value}")
    ev = _append(cfg, key, E.PHASE_CHANGED, payload, actor=actor, sid=sid, expect=expect)
    if forced:
        _append(cfg, key, E.NOTE,
                {"text": f"forced past {unverified} unverified acceptance criteria by {actor}"},
                actor=actor, sid=step_id(key, snap.phase, snap.observed_seq, "force-ac-override"))
    elif phase == Phase.AWAITING_CI:
        _warn_unverified_acs(cfg, key, actor=actor)
    if requeue_in is not None:
        requeue(cfg, key, requeue_in, actor=actor)
    return ev


def _acs_unverified_count(cfg: Config, key: str, snap) -> int:
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        return 0
    return snap.acs_unverified(spec_path.read_text(encoding="utf-8"))


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


def local_write_backup(cfg: Config, key: str, *, actor: str = "reconciler",
                       now: float | None = None) -> str | None:
    """AD-6: snapshot *key*'s resolved target directory before the reconciler
    writes into it in place -- the compensating control a ``mode = "local"``
    repo binding uses in place of the PR review checkpoint a git binding gets.
    No-op (returns None) unless the ticket resolves to a local-mode binding
    with an existing path.

    Idempotent per reconcile step: the backup is recorded under a step-id keyed
    on ``(phase, observed_seq)``, so a crash-and-respawn mid-step (after the
    tarball was already taken) does not create a second one -- it just resumes
    writing against the same backup.
    """
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode != "local" or not binding.path:
        return None
    target = Path(binding.path)
    if not target.exists():
        return None
    snap = snap_mod.load(cfg.home, key)
    sid = step_id(key, snap.phase, snap.observed_seq, "local-write-backup")
    if any(e.get("step_id") == sid for e in event_log.read(cfg.home, key)):
        return None  # already backed up this step
    now = now if now is not None else store.now_epoch()
    archive = backup.backup_local_target(target, now)
    _append(cfg, key, E.NOTE, {"text": f"local write backup: {archive}"}, actor=actor, sid=sid)
    return str(archive)


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
    """
    if verdict not in QA_VERDICTS:
        raise store.MaestroError(f"{key}: --verdict must be one of {sorted(QA_VERDICTS)}, got {verdict!r}")
    if axis not in QA_AXES:
        raise store.MaestroError(f"{key}: --axis must be one of {sorted(QA_AXES)}, got {axis!r}")
    spec_path = store.spec_path(cfg.home, key)
    if not spec_path.exists():
        raise store.MaestroError(f"{key}: no spec.md to verify ACs against")
    acs = snap_mod.parse_acs(spec_path.read_text(encoding="utf-8"))
    if not (1 <= ac_index <= len(acs)):
        raise store.MaestroError(f"{key}: AC #{ac_index} out of range (spec has {len(acs)} AC(s))")
    ac_text = acs[ac_index - 1]
    h = snap_mod.ac_hash(ac_text)
    snap = snap_mod.load(cfg.home, key)
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
    set_phase(cfg, key, Phase.AWAITING_HUMAN, reason="asked human", actor=actor)
    return qid


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
              reason=f"resolve merge conflict for PR #{pr_number}", actor=actor)
    return True


def route_stale(cfg: Config, key: str, *, base_branch: str = "main",
                actor: str = "dispatcher") -> bool:
    """Route a ticket whose worktree has drifted behind its repo's base branch
    back into `implementing` so the reconciler rebases (idempotent — a no-op if
    already implementing). Mirrors `route_conflict`'s auto-resolution path, but
    fires from the dispatcher's proactive drift check
    (`dispatcher.sync_worktrees`) rather than a GitHub-reported CONFLICTING PR.
    `base_branch` names the ticket's actual resolved repo binding (`main` for
    an unbound ticket / single-repo board). Returns True if it moved the
    ticket."""
    snap = snap_mod.load(cfg.home, key)
    if snap.phase == Phase.IMPLEMENTING.value:
        return False
    set_phase(cfg, key, Phase.IMPLEMENTING,
              reason=f"origin/{base_branch} advanced — rebase worktree onto latest {base_branch}",
              actor=actor)
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


def approve(cfg: Config, key: str, *, actor: str = "human") -> None:
    """Clear the tier-2 implementing gate (idempotent -- a fixed step-id means
    repeat calls are a no-op). Once appended, `dispatcher.is_due` finds
    `snap.approved` and the ticket is due on the very next sweep."""
    _append(cfg, key, E.APPROVED, {}, actor=actor, sid=f"approve-{key}")


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


def fail(cfg: Config, key: str, error: str, *, actor: str = "reconciler") -> str:
    """Record a failure; back off, or dead-letter if over the threshold."""
    snap = snap_mod.load(cfg.home, key)
    _append(cfg, key, E.FAILED, {"error": error}, actor=actor,
            sid=step_id(key, snap.phase, snap.observed_seq, "fail"))
    snap = snap_mod.load(cfg.home, key)
    if snap.failure_count >= cfg.max_failures:
        _append(cfg, key, E.STALLED, {"reason": f"{snap.failure_count} failures: {error}"},
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
            stem = lp.removesuffix(".stream.jsonl").removesuffix(".log")
            live_paths.add(stem + ".log")
            live_paths.add(stem + ".stream.jsonl")

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

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as f:
            for ev in to_archive:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")

        # Rewrite active log with only post-snapshot events.
        tmp = active_path.parent / f".{active_path.name}.compact.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            for ev in post:
                f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        tmp.replace(active_path)

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
