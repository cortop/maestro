"""Deterministic base sync, PR opening and next-phase routing for an implementing ticket."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import events as E
from . import implementing_brief, ops, repos as repos_mod, snapshot as snap_mod, store
from .config import Config
from .statemachine import Phase

_TIMEOUT = 300

# round -> (route when the PR is a draft, route when it is ready).
# T-209: a draft PR's CI-fix round is a new diff nobody has judged, so it goes to `qa`.
# Review-feedback and conflict/drift rounds deliberately skip `qa`: a human is already
# reviewing the PR on GitHub, and the spec (so the attestations) did not change.
_ROUTES = {
    "fresh": ("qa", "qa"),
    "split-decision": ("qa", "qa"),
    "qa-fix": ("qa", "qa"),
    "ci-fix": ("qa", "awaiting-ci"),
    "review-feedback": ("awaiting-ci", "awaiting-ci"),
    "conflict": ("awaiting-ci", "awaiting-ci"),
    "base-drift": ("awaiting-ci", "awaiting-ci"),
}


def _run(args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    # *env* is a credential overlay; passed alone it would drop PATH.
    run_env = {**os.environ, **env} if env else None
    try:
        return subprocess.run(args, cwd=cwd, env=run_env, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise store.MaestroError(f"`{' '.join(args[:3])} ...` failed to run: {exc}") from exc


def _must(args: list[str], what: str, **kw) -> subprocess.CompletedProcess:
    p = _run(args, **kw)
    if p.returncode != 0:
        raise store.MaestroError(f"{what} failed (rc={p.returncode}): {(p.stderr or p.stdout).strip()[:500]}")
    return p


def _worktree(cfg: Config, key: str) -> tuple[Path, repos_mod.RepoBinding]:
    binding = repos_mod.resolve(cfg, cfg.home, key)
    if binding.mode == "local":
        raise store.MaestroError(f"{key}: local-mode tickets have no branch or PR")
    wt = store.worktree_path(cfg.home, key)
    if not wt.exists():
        raise store.MaestroError(f"{key}: no worktree at {wt} (run `maestro worktree ensure {key}`)")
    return wt, binding


def _unmerged(wt: Path) -> list[str]:
    p = _run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"])
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _conflicted(files: list[str], mode: str, cont: str) -> dict:
    return {"status": "conflicted", "mode": mode, "conflicted_files": files, "continue_command": cont}


def sync_base(cfg: Config, key: str) -> dict:
    """Incorporate the remote PR branch, then the base branch; report clean or conflicted.

    Never aborts and never force-pushes: a conflict stays in the tree for the caller to resolve.
    """
    wt, binding = _worktree(cfg, key)
    branch = f"{binding.branch_prefix}{key}"
    base = binding.base_branch or "main"
    w = str(wt)
    if repos_mod.resolve_stack_tool(binding) == "gt":
        _must(["gt", "--cwd", w, "sync"], "gt sync")
        r = _run(["gt", "--cwd", w, "restack"])
        if r.returncode == 0:
            return {"status": "clean", "mode": "gt", "conflicted_files": [], "continue_command": None}
        files = _unmerged(wt)
        if not files:
            raise store.MaestroError(f"gt restack failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[:500]}")
        return _conflicted(files, "gt", f"gt --cwd {w} continue")

    pushed = _run(["git", "-C", w, "fetch", "-q", "origin", branch]).returncode == 0
    if pushed:
        if _run(["git", "-C", w, "merge", "-q", "--ff-only", f"origin/{branch}"]).returncode != 0:
            r = _run(["git", "-C", w, "merge", "-q", f"origin/{branch}"])
            if r.returncode != 0:
                files = _unmerged(wt)
                if not files:
                    raise store.MaestroError(f"merge of origin/{branch} failed: {(r.stderr or r.stdout).strip()[:500]}")
                return _conflicted(files, "merge", f"git -C {w} commit --no-edit")
    _must(["git", "-C", w, "fetch", "-q", "origin", base], f"fetch origin {base}")
    if pushed:
        mode, cmd, cont = "merge", ["merge", "-q", f"origin/{base}"], f"git -C {w} commit --no-edit"
    else:
        mode, cmd, cont = "rebase", ["rebase", f"origin/{base}"], f"git -C {w} rebase --continue"
    r = _run(["git", "-C", w, *cmd])
    if r.returncode == 0:
        return {"status": "clean", "mode": mode, "conflicted_files": [], "continue_command": None}
    files = _unmerged(wt)
    if not files:
        raise store.MaestroError(f"git {cmd[0]} origin/{base} failed: {(r.stderr or r.stdout).strip()[:500]}")
    return _conflicted(files, mode, cont)


def _gh_env(binding) -> dict | None:
    from .dispatcher import resolve_credential  # lazy: avoid a module-load cycle
    cred = resolve_credential(binding, {})
    if not cred.ok:
        raise store.MaestroError(f"credential resolution failed ({cred.error})")
    return cred.env


def _repo_args(binding) -> list[str]:
    return ["--repo", binding.slug] if binding.slug else []


def _pr_info(binding, branch: str, env, cwd: Path) -> dict | None:
    p = _run(["gh", "pr", "view", branch, *_repo_args(binding), "--json", "number,url"], cwd=cwd, env=env)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    return data if data.get("number") else None


def _adopt_or_create(binding, wt: Path, env, *, head: str, base: str, title: str, body: str) -> dict:
    info = _pr_info(binding, head, env, wt)
    if info is None:
        _must(["gh", "pr", "create", *_repo_args(binding), "--base", base, "--head", head, "--draft",
               "--title", title, "--body", body], f"gh pr create ({head})", cwd=wt, env=env)
        info = _pr_info(binding, head, env, wt)
        if info is None:
            raise store.MaestroError(f"gh pr create ({head}) succeeded but the PR cannot be read back")
    return info


def _record(cfg: Config, key: str, info: dict, *, stack: dict | None, sid: str, actor: str) -> bool:
    payload = {"number": info["number"], "url": info["url"], "draft": True}
    if stack:
        payload["stack"] = stack
    ev = ops._append(cfg, key, E.PR_OPENED, payload, actor=actor, sid=sid)
    return ev is not None


def open_pr(cfg: Config, key: str, *, title: str, body: str, stack: list[dict] | None = None,
            actor: str = "reconciler") -> dict:
    """Push, create or adopt the draft PR(s), and record `PrOpened`; safe to re-run.

    *stack* is an ordered list of ``{"title", "body", "sha"}`` entries (``sha`` is the entry's
    last commit; omitted for the final entry, which sits at HEAD). Entries open root-first.
    Titles and bodies come from the caller: the PR's wording is judgment, the plumbing is not.
    """
    wt, binding = _worktree(cfg, key)
    prefix, base = binding.branch_prefix, binding.base_branch or "main"
    branch, w = f"{prefix}{key}", str(wt)
    env = _gh_env(binding)
    if not stack:
        _must(["git", "-C", w, "push", "-q", "-u", "origin", branch], "git push")
        info = _adopt_or_create(binding, wt, env, head=branch, base=base, title=title, body=body)
        recorded = _record(cfg, key, info, stack=None, sid=f"pr-{key}", actor=actor)
        return {"opened": [info], "recorded": recorded}

    total = len(stack)
    if total < 2:
        raise store.MaestroError("open-pr: a stack needs at least two entries")
    for i, e in enumerate(stack):
        if not e.get("title") or not e.get("body") or (i < total - 1 and not e.get("sha")):
            raise store.MaestroError(f"open-pr: stack entry {i} needs title, body and (except the last) sha")
    gt = repos_mod.resolve_stack_tool(binding) == "gt"
    branches = [f"{prefix}{key}-{i + 1}" if i < total - 1 else branch for i in range(total)]
    bases = [base, *branches[:-1]]
    if gt:
        for i, e in enumerate(stack[:-1]):
            _must(["git", "-C", w, "checkout", "-q", e["sha"]], "git checkout")
            _must(["gt", "--cwd", w, "create", branches[i], "-m", e["title"]], "gt create")
        _must(["git", "-C", w, "checkout", "-q", branch], "git checkout")
        _must(["gt", "--cwd", w, "submit", "--stack", "--no-interactive", "--publish"], "gt submit")
    opened, recorded = [], []
    for i, e in enumerate(stack):  # root-first
        if gt:
            info = _pr_info(binding, branches[i], env, wt)
            if info is None:
                raise store.MaestroError(f"gt submit left no PR for {branches[i]}")
            _must(["gh", "pr", "edit", str(info["number"]), *_repo_args(binding), "--title", e["title"],
                   "--body", e["body"]], "gh pr edit", cwd=wt, env=env)
        else:
            if i < total - 1:
                _run(["git", "-C", w, "branch", branches[i], e["sha"]])  # exists on a re-run
            _must(["git", "-C", w, "push", "-q", "-u", "origin", branches[i]], "git push")
            info = _adopt_or_create(binding, wt, env, head=branches[i], base=bases[i],
                                    title=e["title"], body=e["body"])
        meta = {"index": i, "total": total, "branch": branches[i], "base": bases[i]}
        sid = f"pr-{key}" if i == 0 else f"pr-stack-{key}-{i}"
        recorded.append(_record(cfg, key, info, stack=meta, sid=sid, actor=actor))
        opened.append(info)
    return {"opened": opened, "recorded": any(recorded)}


def handoff(cfg: Config, key: str, *, summary: str | None = None, actor: str = "reconciler") -> dict:
    """Move the ticket to `qa` or `awaiting-ci`, chosen from the implementing round and `pr_draft`.

    A non-blank `summary` is appended to the reason with newlines collapsed to spaces.
    """
    round_name, reason = implementing_brief.current_round(cfg.home, key)
    if round_name not in _ROUTES:
        raise store.MaestroError(
            f"{key}: handoff: cannot tell which implementing round this is (reason: {reason!r}); "
            f"pick the phase yourself with `maestro set-phase`")
    snap = snap_mod.load(cfg.home, key)
    if snap.pr_number is None:
        raise store.MaestroError(f"{key}: handoff: no PR is recorded yet -- run `maestro open-pr {key}` first")
    to = _ROUTES[round_name][0 if snap.pr_draft is not False else 1]
    reason = f"{round_name} round done"
    text = " ".join((summary or "").split())
    if text:
        reason = f"{reason}: {text}"
    ev = ops.set_phase(cfg, key, Phase(to), reason=reason, actor=actor)
    return {"round": round_name, "pr_draft": snap.pr_draft, "to": to, "moved": ev is not None}
