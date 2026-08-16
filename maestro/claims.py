"""Per-key claims — the dispatcher's dedup hint, backed by VERIFIED process identity.

A reconciler is launched as a detached ``claude -p`` process (headless sessions do
not appear in ``claude agents --json``), so liveness is tracked by a small claim file
holding the worker's pid. A bare "is *a* process with this pid alive?" is not enough:
darwin's pid counter wraps well below ``kern.maxproc``, so a claim can outlive its
reconciler and then be "confirmed" alive again once the pid is reused by an unrelated
process, deadlocking the key's dispatch slot forever.

``verify_claim``/``probe_processes`` instead compute a three-valued verdict —
``"confirmed"`` / ``"denied"`` / ``"unknown"`` — by matching a live process's start
time (from ``ps -ww -o pid=,etime=,command=``, the stdlib-only, injectable-via-``run``
external boundary) against the ``epoch`` the claim recorded at write time: pid reuse
necessarily means the live process started AFTER the claim's epoch, so a start time
later than ``epoch`` (beyond a small tolerance) is ``"denied"``. ``"unknown"`` (no
``ps``, unparsable output, or a probe error) falls back to the old liveness check,
capped by ``unverified_claim_max_age`` so an unverifiable claim can't squat a slot
forever either. ``active_keys`` probes ONCE for the whole batch of claims per call.

This is why the claim is only a *hint*: the event log's fencing token (RB-7 — armed on
`ops.set_phase`, the state-machine gate) is the real authority against a phase transition
decided from a fold that went stale before the write landed, e.g. during the grace window
above, or a human racing a live reconciler.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import store

# Generous stdlib-only default (T-17): unverifiable claims (no `ps`, unparsable
# etime, probe error) fall back to raw pid liveness, but only up to this ceiling —
# past it the claim is released with no kill and no FAILED event. Deliberately far
# above any real session length so it never races T-13's max_session_seconds watchdog.
DEFAULT_UNVERIFIED_CLAIM_MAX_AGE = 24 * 3600

# A pid this large can never be real on any platform maestro targets, but `ps -p`
# aborts its ENTIRE query on a single out-of-range pid ("process id too large"),
# silently losing the verdict for every other pid in the batch. Sanitize before
# building the `ps` argument list rather than let one bad claim poison the sweep.
_MAX_SANE_PID = 4_194_304

# Tolerance for clock/measurement slop between the claim's recorded `epoch` and the
# process start time we back out of `ps`'s `etime` (whole-second granularity, plus
# the small delay between a process forking and `write_claim` running).
_EPOCH_TOLERANCE_SECONDS = 5

_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def claim_path(home: Path, key: str) -> Path:
    return home / "derived" / "claims" / f"{store.validate_key(key)}.json"


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        # kill(pid, 0) with pid <= 0 targets a process GROUP (or, for -1, every
        # process the caller may signal), not one process — never treat it as a
        # single live pid, and never let it reach a real signal elsewhere.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def write_claim(home: Path, key: str, pid: int, name: str,
                *, log_path: str | None = None, cwd: str | None = None,
                prompt: str | None = None, runner: str | None = None,
                kind: str | None = None) -> None:
    data: dict = {"pid": pid, "name": name, "ts": store.iso_now(),
                  "epoch": store.now_epoch()}
    if log_path is not None:
        data["log_path"] = log_path
    # T-45: the resolved reconcile command lives inside `prompt` (dispatcher
    # builds it as f"{command} {key}") and `cwd` is the session's worktree --
    # both cheap to stash here at spawn time so a later sweep, finding the
    # process already dead, can name them in a `Failed` event without having
    # to re-derive them (the ticket's phase may not even be the same anymore).
    if cwd is not None:
        data["cwd"] = cwd
    if prompt is not None:
        data["prompt"] = prompt
    # T-54: the runner this claim's session was actually spawned under (RF-2
    # name, e.g. "claude"/"opencode") -- the fact `_runner_concurrency_cap`'s
    # active count is derived from, instead of re-resolving `resolve_runner`
    # against the key's CURRENT phase every sweep (which silently stops
    # counting a session the moment its key folds to a phase where that
    # runner is no longer eligible; see `dispatcher.dispatch`'s cap check).
    if runner is not None:
        data["runner"] = runner
    # RB-14: distinguishes a dispatcher-owned test-run subprocess's claim
    # (kind="testrun") from an ordinary reconciler session's -- the ONLY new
    # field a non-LLM claim holder needed; `_verdict`/`_evaluate` still key
    # purely on pid + epoch (see this module's docstring), so liveness/dedup
    # stays exactly as runner-agnostic as it already was for `runner` above.
    if kind is not None:
        data["kind"] = kind
    store.write_json(claim_path(home, key), data)


def read_claim(home: Path, key: str) -> dict | None:
    return store.read_json(claim_path(home, key))


def release(home: Path, key: str) -> None:
    try:
        claim_path(home, key).unlink()
    except FileNotFoundError:
        pass


def _sanitize_pid(pid) -> int | None:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p > _MAX_SANE_PID:
        return None
    return p


def _parse_etime(etime: str) -> float | None:
    """Parse `ps`'s locale-free `[[dd-]hh:]mm:ss` into elapsed seconds."""
    m = _ETIME_RE.match(etime.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    total = int(minutes) * 60 + int(seconds)
    if hours is not None:
        total += int(hours) * 3600
    if days is not None:
        total += int(days) * 86400
    return float(total)


def probe_processes(pids, *, run=subprocess.run) -> dict[int, dict]:
    """Probe live processes for start time + command in ONE ``ps`` call.

    Returns ``{pid: {"start_epoch": float, "command": str}}`` for pids the probe
    found and could parse. Malformed/out-of-range pids are dropped before the call
    so one bad pid can't abort ``ps`` for the whole batch; any probe failure
    (missing ``ps``, non-zero exit, unparsable ``etime``) yields ``{}`` rather than
    raising — callers must treat an absent pid as verdict ``"unknown"``, never
    ``"denied"``.
    """
    sanitized = sorted({p for p in (_sanitize_pid(x) for x in pids) if p is not None})
    if not sanitized:
        return {}
    pid_arg = ",".join(str(p) for p in sanitized)
    try:
        proc = run(["ps", "-ww", "-o", "pid=,etime=,command=", "-p", pid_arg],
                   capture_output=True, text=True)
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    now = store.now_epoch()
    out: dict[int, dict] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        secs = _parse_etime(parts[1])
        if secs is None:
            continue
        out[pid] = {"start_epoch": now - secs, "command": parts[2] if len(parts) > 2 else ""}
    return out


def _verdict(claim: dict, probe: dict[int, dict]) -> str:
    """Three-valued verdict for one claim against a (possibly batched) probe result."""
    pid = _sanitize_pid(claim.get("pid"))
    if pid is None:
        return "unknown"
    info = probe.get(pid)
    if info is None:
        return "unknown"
    epoch = claim.get("epoch")
    if epoch is None:
        return "unknown"
    if info["start_epoch"] > float(epoch) + _EPOCH_TOLERANCE_SECONDS:
        return "denied"  # pid reuse: the live process started AFTER this claim
    return "confirmed"


def _evaluate(claim: dict, probe: dict[int, dict], *,
              max_age: float, now: float) -> tuple[bool, str]:
    """(still_claimed, verdict) for one claim. Denied releases immediately; unknown
    falls back to raw pid liveness, capped by ``max_age`` since the claim's epoch."""
    verdict = _verdict(claim, probe)
    if verdict == "denied":
        return False, verdict
    if verdict == "confirmed":
        return True, verdict
    epoch = claim.get("epoch")
    age_ok = epoch is not None and (now - float(epoch)) <= max_age
    return (pid_alive(claim.get("pid")) and age_ok), verdict


def verify_claim(home: Path, key: str, *, run=subprocess.run) -> str:
    """Return ``"confirmed"`` / ``"denied"`` / ``"unknown"`` for ``key``'s claim.

    No claim on disk -> ``"unknown"``. Does a one-off, single-pid probe; batch
    callers (``active_keys``) should probe once themselves and use ``_verdict``.
    """
    claim = read_claim(home, key)
    if not claim:
        return "unknown"
    pid = _sanitize_pid(claim.get("pid"))
    probe = probe_processes([pid] if pid is not None else [], run=run)
    return _verdict(claim, probe)


def is_claimed(home: Path, key: str, *, run=subprocess.run,
               max_age: float = DEFAULT_UNVERIFIED_CLAIM_MAX_AGE) -> bool:
    claim = read_claim(home, key)
    if not claim:
        return False
    pid = _sanitize_pid(claim.get("pid"))
    probe = probe_processes([pid] if pid is not None else [], run=run)
    claimed, _verdict_ = _evaluate(claim, probe, max_age=max_age, now=store.now_epoch())
    if not claimed:
        release(home, key)  # denied, or unknown+stale/over-age — reclaim
    return claimed


def describe_claims(home: Path, *, run=subprocess.run,
                     max_age: float = DEFAULT_UNVERIFIED_CLAIM_MAX_AGE) -> list[dict]:
    """One row per claim file: key, pid, age (seconds since ``epoch``), verdict, and
    whether it would still count as ``claimed`` (i.e. survive a real sweep).

    The data source for ``maestro claims``; ``active_keys`` and ``--purge`` both
    build on the same per-row ``claimed`` decision so listing and purging never
    disagree about which claims are stale. Probes the whole batch exactly once.
    """
    d = home / "derived" / "claims"
    if not d.exists():
        return []
    claims_by_key: dict[str, dict] = {}
    pids: list[int] = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("."):
            continue
        c = read_claim(home, f.stem)
        if not c:
            continue
        claims_by_key[f.stem] = c
        pid = _sanitize_pid(c.get("pid"))
        if pid is not None:
            pids.append(pid)
    probe = probe_processes(pids, run=run)
    now = store.now_epoch()
    rows = []
    for key, c in sorted(claims_by_key.items()):
        claimed, verdict = _evaluate(c, probe, max_age=max_age, now=now)
        epoch = c.get("epoch")
        age = round(now - float(epoch)) if epoch is not None else None
        rows.append({"key": key, "pid": c.get("pid"), "age_s": age,
                     "verdict": verdict, "claimed": claimed})
    return rows


def active_keys(home: Path, *, run=subprocess.run,
                max_age: float = DEFAULT_UNVERIFIED_CLAIM_MAX_AGE) -> set[str]:
    out: set[str] = set()
    for row in describe_claims(home, run=run, max_age=max_age):
        if row["claimed"]:
            out.add(row["key"])
        else:
            release(home, row["key"])
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
