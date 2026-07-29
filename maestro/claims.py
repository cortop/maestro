"""Per-key claims — the dispatcher's dedup hint, backed by PROCESS LIVENESS.

A reconciler is launched as a detached ``claude -p`` process (headless sessions do
not appear in ``claude agents --json``), so liveness is tracked by a small claim file
holding the worker's pid. The dispatcher treats a key as claimed iff a claim file
exists AND its pid is still alive — it checks live processes, never a TTL countdown.
A crashed worker leaves a stale claim whose pid is dead; the next sweep reclaims it.
This is why the claim is only a *hint*: the event log's fencing token is the real
authority against double-writes.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import store


def claim_path(home: Path, key: str) -> Path:
    return home / "derived" / "claims" / f"{store.validate_key(key)}.json"


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def write_claim(home: Path, key: str, pid: int, name: str,
                *, log_path: str | None = None) -> None:
    data: dict = {"pid": pid, "name": name, "ts": store.iso_now(),
                  "epoch": store.now_epoch()}
    if log_path is not None:
        data["log_path"] = log_path
    store.write_json(claim_path(home, key), data)


def read_claim(home: Path, key: str) -> dict | None:
    return store.read_json(claim_path(home, key))


def release(home: Path, key: str) -> None:
    try:
        claim_path(home, key).unlink()
    except FileNotFoundError:
        pass


def is_claimed(home: Path, key: str) -> bool:
    c = read_claim(home, key)
    if not c:
        return False
    if pid_alive(c.get("pid")):
        return True
    release(home, key)  # stale — reclaim
    return False


def active_keys(home: Path) -> set[str]:
    d = home / "derived" / "claims"
    out: set[str] = set()
    if d.exists():
        for f in d.glob("*.json"):
            if f.name.startswith("."):
                continue
            if is_claimed(home, f.stem):
                out.add(f.stem)
    return out


def all_claims(home: Path) -> dict[str, dict]:
    """Every claim file's raw contents, keyed by ticket key -- regardless of pid
    liveness. Used by the watchdog, which reaps on session AGE, not liveness."""
    d = home / "derived" / "claims"
    out: dict[str, dict] = {}
    if d.exists():
        for f in d.glob("*.json"):
            if f.name.startswith("."):
                continue
            c = store.read_json(f)
            if c:
                out[f.stem] = c
    return out
