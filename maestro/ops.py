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
from . import event_log, inbox, repos as repos_mod, schedule as schedule_mod, snapshot as snap_mod, store
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
_GIT_TIMEOUT = 30       # seconds; worktree add/adopt and rev-parse plumbing calls
_DEFAULT_PRIME_TIMEOUT = 600  # seconds; a hanging `npm ci` etc. must not wedge a reconciler

# GA-7-derived: gitignored-by-convention names a fresh `git worktree add` never
# brings (tracked files only) that this op mirrors from the source checkout.
_PRIMED_EXTRA_FILES = ("CLAUDE.local.md", ".claude/settings.local.json")
_PRIMED_NODE_MODULES = "node_modules"


def _git_dir(wt: Path) -> Path:
    """The worktree-SPECIFIC git dir (e.g. ``<repo>/.git/worktrees/<KEY>``) --
    distinct from ``--git-common-dir``/``--git-path``, which resolve to the
    dir SHARED by every worktree of the repo. Self-cleaning: `git worktree
    remove` (dispatcher.py's merge-triggered cleanup) deletes this whole
    directory, so anything stored under it never outlives its worktree."""
    out = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-dir"],
                         capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=True).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else wt / p


def _worktree_create_or_adopt(repo: str, wt: Path, branch: str, base: str) -> None:
    """Create *wt* on a fresh branch off ``origin/<base>``, or adopt *branch*
    if it already exists -- the same create-then-fallback the old ready.md
    prose did. Raises loudly (never a silent no-op) if both attempts fail."""
    fetch = subprocess.run(["git", "-C", repo, "fetch", "-q", "origin", base],
                           capture_output=True, text=True, timeout=_FETCH_TIMEOUT)
    if fetch.returncode != 0:
        raise store.MaestroError(
            f"git fetch origin {base!r} in {repo} failed (rc={fetch.returncode}): "
            f"{fetch.stderr.strip()}")
    created = subprocess.run(
        ["git", "-C", repo, "worktree", "add", str(wt), "-b", branch, f"origin/{base}"],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    if created.returncode != 0:
        adopted = subprocess.run(
            ["git", "-C", repo, "worktree", "add", str(wt), branch],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT)
        if adopted.returncode != 0:
            raise store.MaestroError(
                f"git worktree add failed for {wt} in {repo}: create "
                f"({created.stderr.strip()}) and adopt ({adopted.stderr.strip()}) both failed")


def _prime_worktree_extras(repo: str, wt: Path) -> None:
    """GA-7, absorbed: exclude the mirrored names from `git add` via the
    git-COMMON `info/exclude` (idempotent -- shared across every worktree of
    the repo), then mirror CLAUDE.local.md / .claude/settings.local.json /
    node_modules from the source checkout into *wt*. A real, write-isolated
    copy (never a symlink or hardlink -- see the `cp` ladder below), so an
    install run inside *wt* never writes through into *repo* or a sibling
    worktree. Every step is a no-op when the source doesn't have it."""
    exclude = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=True).stdout.strip()
    exclude_path = Path(exclude)
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
    if src_nm.is_dir() and not dst_nm.exists():
        # cp -c is an APFS copy-on-write clone (write-isolated, near-instant);
        # --reflink=auto is its GNU/Linux equivalent (CoW on btrfs/xfs, falling
        # back to a deep copy itself when unsupported); cp -R is the last-resort
        # deep copy for any other cp. Deliberately no hardlink rung -- a
        # hardlink shares one inode, so an in-place write mutates both copies
        # at once (not write-isolated). The trailing "/." copies node_modules'
        # *contents* into place instead of nesting a second node_modules inside it.
        rungs = (
            ["cp", "-c", "-R", f"{src_nm}/.", str(dst_nm)],
            ["cp", "--reflink=auto", "-R", f"{src_nm}/.", str(dst_nm)],
            ["cp", "-R", f"{src_nm}/.", str(dst_nm)],
        )
        for rung in rungs:
            if subprocess.run(rung, capture_output=True, text=True).returncode == 0:
                break
        else:
            raise store.MaestroError(f"node_modules copy into {dst_nm} failed on every cp rung")


def _run_prime(binding: repos_mod.RepoBinding, wt: Path, repo: str, key: str, timeout: int) -> None:
    env = {**os.environ, "WT": str(wt), "REPO": repo, "KEY": key}
    try:
        result = subprocess.run(binding.prime, shell=True, cwd=wt, env=env,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise store.MaestroError(
            f"prime for repo {binding.name!r} ({key}) exceeded its {timeout}s timeout")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise store.MaestroError(
            f"prime for repo {binding.name!r} ({key}) failed (rc={result.returncode}): {detail}")


def worktree_ensure(cfg: Config, key: str, *, prime_timeout: int = _DEFAULT_PRIME_TIMEOUT) -> dict:
    """Idempotently create-or-adopt *key*'s reconciler worktree, absorb GA-7's
    CLAUDE.local.md/.claude/settings.local.json/node_modules priming, and run
    the resolved repo binding's `prime` command exactly once inside it.

    `prime` comes ONLY from ``repos.resolve()`` (config.toml's ``[repos.<name>]
    prime`` or the ``[maestro] prime`` fallback) -- never a spec front-matter
    field or a ``TicketCreated`` payload, neither of which this function or
    anything it calls ever reads.

    Idempotence: a fresh ``git worktree add`` is skipped once the worktree dir
    already exists; `prime` is skipped once its worktree-local marker (under
    the worktree's OWN git dir -- see `_git_dir` -- so it self-cleans when the
    worktree is removed) is present. Raises ``store.MaestroError`` -- never a
    silent success -- if worktree add/adopt fails, or if `prime` exits non-zero
    or exceeds *prime_timeout*; the CLI wrapper surfaces that as a non-zero
    exit with the repo name and rc in stderr, and appends no event, so no
    phase transition follows a failed ensure.

    AD-6 ``mode = "local"`` bindings have no worktree at all: a successful,
    side-effect-free no-op.
    """
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode == "local":
        return {"created": False, "primed": False}
    if not binding.path:
        raise store.MaestroError(f"{key}: repo binding {binding.name!r} has no path configured")

    repo = binding.path
    wt = store.worktree_path(cfg.home, key)
    created = False
    if not wt.exists():
        branch = f"{binding.branch_prefix}{key}"
        _worktree_create_or_adopt(repo, wt, branch, binding.base_branch)
        created = True

    _prime_worktree_extras(repo, wt)

    primed = False
    if binding.prime:
        marker = _git_dir(wt) / "maestro-primed"
        if not marker.exists():
            _run_prime(binding, wt, repo, key, prime_timeout)
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
    acs = snap_mod.parse_acs(spec_file.read_text(encoding="utf-8"))
    if not acs:
        raise store.MaestroError(f"{key}: spec.md has no acceptance criteria to check")

    binding = repos_mod.resolve(cfg, cfg.home, key)
    base = binding.base_branch
    cwd = _worker_cwd(cfg, key)
    diff, base_ref, ahead, stderr = _qa_diff(cwd, base)
    return {
        "key": key,
        "acs": [{"index": i, "text": t, "ac_hash": snap_mod.ac_hash(t)}
                for i, t in enumerate(acs, start=1)],
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
    set_phase(cfg, key, Phase.AWAITING_HUMAN,
              reason=f"asked human ({total} question(s) this round)", actor=actor)
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
        # also picks up atomic_write's pid-suffixed tmp name, so two concurrent
        # compactions of the same key (impossible today under file_lock, but a
        # future caller) can't collide on a fixed ".compact.tmp" name.
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
