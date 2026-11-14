"""Cross-ticket stacking: start a ticket on its dependency's branch, then retarget it or ask a human once that dependency is done."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import repos as repos_mod
from . import snapshot as snap_mod
from . import store
from .config import Config
from .statemachine import Phase

_LS_REMOTE_TIMEOUT = 30

# Phases a merged dependency routes back to `implementing` for the base sync. READY and
# IMPLEMENTING sync on their own; DEGRADED and TERMINATING are left untouched.
_ROUTE_ON_MERGE = frozenset({Phase.QA, Phase.VERIFYING, Phase.AWAITING_CI, Phase.IN_REVIEW,
                             Phase.AWAITING_HUMAN})
_UNRESOLVABLE = frozenset({Phase.DONE, Phase.DEGRADED, Phase.TERMINATING})


def unfinished_deps(home: Path, key: str) -> list[str]:
    """The spec's `dependsOn` entries that have not reached `done`."""
    from .dispatcher import parse_depends_on  # lazy: dispatcher imports this module
    spec_file = store.spec_path(home, key)
    if not spec_file.exists():
        return []
    deps = parse_depends_on(spec_file.read_text(encoding="utf-8"))
    return [d for d in deps if snap_mod.load(home, d).phase != Phase.DONE.value]


def dep_branch_pushed(cfg: Config, key: str, dep: str) -> bool:
    """True once *dep*'s branch exists on origin of *key*'s repo."""
    binding = repos_mod._resolve_unstacked(cfg, cfg.home, key)
    if not binding.path:
        return False
    branch = repos_mod.dep_branch(cfg, cfg.home, dep)
    try:
        p = subprocess.run(["git", "-C", binding.path, "ls-remote", "--exit-code", "--heads",
                            "origin", branch], capture_output=True, text=True,
                           timeout=_LS_REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def satisfies(cfg: Config, key: str, dep: str) -> bool:
    """True when *key*'s live stack on *dep* lets it start: its branch is pushed."""
    snap = snap_mod.load(cfg.home, key)
    return (snap.stacked_on == dep and not snap.stack_outcome
            and dep_branch_pushed(cfg, key, dep))


def stack(cfg: Config, key: str, *, actor: str = "human") -> dict:
    """Record that *key* starts on its single unfinished dependency's branch.

    Idempotent: a second call appends nothing. Raises `store.MaestroError` (no event) unless
    *key* has exactly one unfinished dependency, no worktree, an implementation kind in a git
    binding, and the same repo binding as that dependency.
    """
    from . import ops
    home = cfg.home
    from .dispatcher import _never_minted  # lazy: dispatcher imports this module
    if _never_minted(home, key):
        raise store.MaestroError(f"unknown ticket {key!r}")
    snap = snap_mod.load(home, key)
    unfinished = unfinished_deps(home, key)
    if snap.stacked_on in unfinished and not snap.stack_outcome:
        return {"key": key, "dep": snap.stacked_on, "recorded": False}
    if len(unfinished) != 1:
        raise store.MaestroError(
            f"{key}: stacking needs exactly one unfinished dependency, found "
            f"{len(unfinished)}" + (f" ({', '.join(unfinished)})" if unfinished else ""))
    dep = unfinished[0]
    if store.worktree_path(home, key).exists():
        raise store.MaestroError(f"{key}: already has a worktree; it cannot be re-based onto {dep}")
    if snap.kind == "research":
        raise store.MaestroError(f"{key}: a research ticket has no branch to stack")
    binding = repos_mod._resolve_unstacked(cfg, home, key)
    if binding.mode == "local":
        raise store.MaestroError(f"{key}: a local-mode ticket has no branch to stack")
    dep_binding = repos_mod._resolve_unstacked(cfg, home, dep)
    if dep_binding.name != binding.name:
        raise store.MaestroError(
            f"{key}: bound to repo {binding.name!r} but {dep} is bound to {dep_binding.name!r}")
    ops.record_ticket_stacked(cfg, key, dep, actor=actor)
    return {"key": key, "dep": dep, "recorded": True}


def _retarget(cfg: Config, key: str, snap) -> bool:
    """Point *key*'s open PR at the repo base branch; True when nothing is left to retarget."""
    if not snap.pr_number:
        return True
    from . import providers
    from .dispatcher import resolve_credential  # lazy: dispatcher imports this module
    binding = repos_mod.resolve(cfg, cfg.home, key)
    cred = resolve_credential(binding, {})
    if not cred.ok:
        return False
    slug = repos_mod.resolve_vcs_slug(cfg, snap) or binding.slug
    result = providers.get_vcs(cfg).set_base(snap.pr_number, binding.base_branch, repo=slug,
                                             env=cred.env)
    return bool(result.get("ok"))


def _resolve(cfg: Config, key: str, snap, dep: str, merged: bool) -> None:
    from . import ops
    phase = Phase(snap.phase)
    if merged:
        if not _retarget(cfg, key, snap):
            return  # retried next sweep; nothing recorded yet
        base = repos_mod.resolve(cfg, cfg.home, key).base_branch
        if phase in _ROUTE_ON_MERGE:
            ops.set_phase(cfg, key, Phase.IMPLEMENTING, actor="dispatcher",
                          reason=f"dependency {dep} merged: {base} advanced -- sync with origin/{base}",
                          expect=snap.observed_seq)
    else:
        qid = f"stack-{key}-{dep}"
        if qid not in snap.open_questions and qid not in snap.answered_questions:
            ops.ask(cfg, key,
                    f"{key} is stacked on {dep}, which ended without merging. Its branch no "
                    f"longer has a base to sync with. Say how to continue (for example, rebase "
                    f"onto the repo base branch and carry on, or discard {key}).",
                    qid=qid, actor="dispatcher")
    ops.record_stack_resolved(cfg, key, dep, "merged" if merged else "unmerged",
                              actor="dispatcher")


def sync(cfg: Config, now: float) -> dict:
    """Resolve every live stack whose dependency has reached `done`."""
    from .dispatcher import list_keys  # lazy: dispatcher imports this module
    resolved: list[str] = []
    for key in list_keys(cfg.home):
        snap = snap_mod.load(cfg.home, key)
        dep = snap.stacked_on
        if not dep or snap.stack_outcome or Phase(snap.phase) in _UNRESOLVABLE:
            continue
        dep_snap = snap_mod.load(cfg.home, dep)
        if dep_snap.phase != Phase.DONE.value:
            continue
        _resolve(cfg, key, snap, dep, dep_snap.pr_state == "merged")
        resolved.append(key)
    return {"resolved": resolved}
