"""Review evidence view-model -- CI timeline and branch diff shared by the TUI and web UI."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config as config_mod, ops, repos, store

DIFF_CAP = 200_000
DIFF_TIMEOUT = 30


def ci_timeline(events: list[dict]) -> list[dict]:
    """One entry per CiObserved event, oldest first: ts, state, failing_checks, failure_excerpt."""
    out = []
    for e in events:
        if e["type"] != "CiObserved":
            continue
        p = e["payload"]
        out.append({"ts": e.get("ts", "?"), "state": p.get("state") or "?",
                    "failing_checks": list(p.get("failing_checks") or []),
                    "failure_excerpt": (p.get("failure_excerpt") or "").rstrip()})
    return out


def ci_timeline_text(events: list[dict]) -> list[str]:
    """`ci_timeline` as plain text blocks (the TUI modal)."""
    blocks = []
    for c in ci_timeline(events):
        line = f"{c['ts']}  {c['state']}"
        if c["failing_checks"]:
            line += "  failing: " + ", ".join(c["failing_checks"])
        if c["failure_excerpt"]:
            line += "\n" + c["failure_excerpt"]
        blocks.append(line)
    return blocks


@dataclass(frozen=True)
class Diff:
    text: str = ""
    truncated: bool = False
    error: str = ""      # "" when the diff ran; else why there is none ("no worktree", git's stderr)


def tree_key(home: Path, key: str) -> str | None:
    """The worktree's current tree key (for current/stale checks); None without a worktree or on git trouble."""
    wt = store.worktree_path(home, key)
    if not wt.is_dir():
        return None
    try:
        timeout = repos.resolve(config_mod.load(str(home)), home, key).worktree_timeout
        return ops._tree_state_key(wt, timeout=timeout)
    except Exception:  # noqa: BLE001 -- a slow or broken git leaves every check "stale"
        return None


def branch_diff(home: Path, key: str, cap: int | None = None, timeout: int = DIFF_TIMEOUT) -> Diff:
    """`git diff origin/<base>...HEAD` in the ticket's worktree, capped at *cap* characters
    (default `DIFF_CAP`)."""
    cap = DIFF_CAP if cap is None else cap
    wt = store.worktree_path(home, key)
    if not wt.is_dir():
        return Diff(error=f"No worktree for {key}.")
    try:
        base = repos.resolve(config_mod.load(str(home)), home, key).base_branch
        r = subprocess.run(["git", "-C", str(wt), "diff", f"origin/{base}...HEAD"],
                           capture_output=True, text=True, timeout=timeout)
    except (store.MaestroError, OSError, subprocess.SubprocessError) as e:
        return Diff(error=str(e) or "git diff failed")
    if r.returncode != 0:
        return Diff(error=r.stderr.strip() or "git diff failed")
    return Diff(text=r.stdout[:cap], truncated=len(r.stdout) > cap)
