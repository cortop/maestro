"""`maestro gc-worktrees`: list, and with apply remove, the worktrees of finished tickets."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import claims, health, repos as repos_mod, store
from .config import Config


def _du(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _is_git_worktree(wt: Path) -> bool:
    return (wt / ".git").is_file()


def _classify(cfg: Config, key: str, wt: Path, force: bool) -> tuple[str, str]:
    """(action, reason) for one dir, before any removal is attempted."""
    if not health.worktree_is_leftover(cfg.home, key):
        return "keep", "ticket is not terminal or has no snapshot"
    if claims.is_claimed(cfg.home, key):
        return "keep", "ticket has a live claim"
    if not _is_git_worktree(wt):
        if force:
            return "remove", "not a git worktree (--force)"
        return "skip", "not a git worktree; removed only with --force"
    return "remove", "leftover of a finished ticket"


def gc_worktrees(cfg: Config, *, apply: bool = False, force: bool = False) -> dict:
    """Report one entry per dir directly under ``<home>/worktrees``; remove with *apply*."""
    entries = []
    removed_bytes = 0
    for wt in health._worktree_dirs(cfg.home):
        key = wt.name
        action, reason = _classify(cfg, key, wt, force)
        size = _du(wt) if action != "keep" else 0
        entry = {"key": key, "action": action, "reason": reason, "bytes": size}
        if action == "remove" and apply:
            entry.update(_remove(cfg, key, wt, force))
            if entry["action"] == "removed":
                removed_bytes += size
        elif action == "remove":
            entry["action"] = "would-remove"
        entries.append(entry)
    return {"applied": apply, "entries": entries,
            "totals": {"removed": sum(e["action"] == "removed" for e in entries),
                       "would_remove": sum(e["action"] == "would-remove" for e in entries),
                       "skipped": sum(e["action"] == "skip" for e in entries),
                       "kept": sum(e["action"] == "keep" for e in entries),
                       "bytes_freed": removed_bytes,
                       "bytes_reclaimable": sum(e["bytes"] for e in entries
                                                if e["action"] in ("would-remove", "removed"))}}


def _remove(cfg: Config, key: str, wt: Path, force: bool) -> dict:
    if not _is_git_worktree(wt):
        import shutil
        shutil.rmtree(wt, ignore_errors=True)
        return {"action": "removed", "reason": "not a git worktree; directory deleted"}
    binding = repos_mod.resolve(cfg, cfg.home, key)
    cmd = ["git", "-C", binding.path, "worktree", "remove", str(wt)]
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "worktree remove failed").strip().splitlines()
        return {"action": "skip", "reason": f"git refused: {msg[-1] if msg else 'unknown'}"}
    return {"action": "removed", "reason": "leftover of a finished ticket"}


def render(report: dict) -> str:
    lines = [f"{e['key']}: {e['action']} -- {e['reason']}" for e in report["entries"]]
    t = report["totals"]
    lines.append(f"total: {t['removed']} removed, {t['would_remove']} would remove, "
                 f"{t['skipped']} skipped, {t['kept']} kept; "
                 f"{t['bytes_freed']} bytes freed, {t['bytes_reclaimable']} bytes reclaimable")
    if not report["applied"] and t["would_remove"]:
        lines.append("dry run: pass --apply to remove")
    return "\n".join(lines)
