"""`maestro fleet` — manage the launchd dispatcher LaunchAgent, plus the
launchctl-free pause/resume kill switch the dispatcher itself consults.

Thin, testable wrapper over the packaged ``install.sh`` plus a status probe. Each
``up``/``down``/``status`` function accepts an injectable ``run`` (defaults to
``subprocess.run``) so the behaviour can be tested against a fake ``launchctl`` /
install script without touching the real system. ``pause``/``resume``/``pause_state``
need no such seam — they are pure ``derived/.paused`` JSON reads/writes via
``store.write_json``/``read_json``.
"""
from __future__ import annotations

import importlib.resources
import os
import re
import subprocess
from pathlib import Path

from . import store

LABEL = "com.maestro.dispatcher"

# Floor on the dispatch cadence, enforced here because ``up`` is the ONLY Python
# path to the install script (the CLI and the TUI fleet panel both route through
# it), and mirrored in daemon/install.sh for direct shell use. Nothing rejected a
# tiny interval before, which is how the fleet ended up sweeping every ~10s on
# 2026-07-19 and spawning 21,731 no-op reconcilers.
MIN_INTERVAL = 60


def _script_path() -> Path:
    # Resolved from package data (maestro/_assets/daemon/) via importlib.resources, so
    # it works from an installed wheel as well as a repo checkout. importlib.resources
    # may extract to a temp path on exotic (e.g. zipped) installs, which can lose the
    # exec bit — restore it defensively.
    ref = importlib.resources.files("maestro") / "_assets" / "daemon" / "install.sh"
    with importlib.resources.as_file(ref) as p:
        path = Path(p)
    if path.exists() and not os.access(path, os.X_OK):
        path.chmod(path.stat().st_mode | 0o111)
    return path


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def clamp_interval(interval) -> int:
    """Coerce a requested cadence to a sane integer at or above ``MIN_INTERVAL``."""
    try:
        val = int(interval)
    except (TypeError, ValueError):
        return MIN_INTERVAL
    return max(MIN_INTERVAL, val)


def up(home: Path, interval: int = 300, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    requested = interval
    interval = clamp_interval(interval)
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    p = run([str(script), "up", "--interval", str(interval)],
            env=env, capture_output=True, text=True)
    out = {"action": "up", "interval": interval, "rc": p.returncode,
           "stdout": (p.stdout or "").strip()}
    if requested != interval:
        out["clamped_from"] = requested
    return out


def down(home: Path, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    p = run([str(script), "down"], env=env, capture_output=True, text=True)
    return {"action": "down", "rc": p.returncode, "stdout": (p.stdout or "").strip()}


def _plist_integer(text: str, key: str) -> int | None:
    m = re.search(rf"<key>{key}</key>\s*<integer>(\d+)</integer>", text)
    return int(m.group(1)) if m else None


def _interval_from_plist(plist=None) -> int | None:
    plist = Path(plist) if plist else _plist_path()
    if not plist.exists():
        return None
    return _plist_integer(plist.read_text(encoding="utf-8"), "StartInterval")


def _throttle_from_plist(plist=None) -> int | None:
    plist = Path(plist) if plist else _plist_path()
    if not plist.exists():
        return None
    return _plist_integer(plist.read_text(encoding="utf-8"), "ThrottleInterval")


def status(home: Path, *, run=subprocess.run, plist=None) -> dict:
    try:
        p = run(["launchctl", "list"], capture_output=True, text=True)
        loaded = p.returncode == 0 and LABEL in (p.stdout or "")
    except FileNotFoundError:
        loaded = False
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(store.now_epoch() - hb["epoch"]) if hb.get("epoch") else None
    pause = pause_state(home, store.now_epoch())
    return {"loaded": loaded, "heartbeat_age_s": age,
            "interval": _interval_from_plist(plist),
            "throttle": _throttle_from_plist(plist), "label": LABEL,
            "paused": pause is not None,
            "pause_since": pause.get("since") if pause else None,
            "pause_until": pause.get("until") if pause else None,
            "pause_reason": pause.get("reason") if pause else None}


# --- launchctl-free kill switch: a single derived/.paused JSON dotfile ------
#
# The dispatcher checks this before ANY other work (mint/sync/scheduled-tasks/
# worktrees/backup/sessions). It is deliberately NOT one of the "known ticket"
# dotfiles ``dispatcher.list_keys`` scans (that only walks ``events/`` and
# ``derived/snapshots/``), so it can never be mistaken for a ticket. Existence
# alone arms the switch — corrupt JSON or an unparseable ``until`` still pause,
# since ``store.read_json`` swallows a decode error and returns the default; a
# kill switch that silently fails open on a truncated write would defeat the
# point of having one.

def pause_path(home: Path) -> Path:
    return home / "derived" / ".paused"


def pause(home: Path, *, until: float | None = None, reason: str | None = None) -> dict:
    """Arm the pause. Idempotent: re-arms over an existing pause, and the
    return value carries the state it just replaced under ``previous``."""
    path = pause_path(home)
    previous = store.read_json(path, None)
    state = {"since": store.now_epoch(), "until": until, "reason": reason}
    store.write_json(path, state)
    out = dict(state)
    if previous is not None:
        out["previous"] = previous
    return out


def resume(home: Path) -> dict:
    """Disarm the pause. Idempotent: exits cleanly even when not paused."""
    path = pause_path(home)
    was_paused = path.exists()
    previous = store.read_json(path, None) if was_paused else None
    path.unlink(missing_ok=True)
    out = {"resumed": True, "was_paused": was_paused}
    if previous is not None:
        out["previous"] = previous
    return out


def pause_state(home: Path, now: float) -> dict | None:
    """``None`` when not paused. A past ``until`` auto-resumes (unlinks the
    file) so it is never a lie on disk; a missing/unparseable ``until`` never
    expires. Corrupt JSON still counts as paused — the file's mere existence
    is what arms it, not a successful parse."""
    path = pause_path(home)
    if not path.exists():
        return None
    raw = store.read_json(path, {}) or {}
    until = raw.get("until")
    if until is not None:
        try:
            until_epoch = float(until)
        except (TypeError, ValueError):
            until_epoch = None
        if until_epoch is not None and until_epoch <= now:
            path.unlink(missing_ok=True)
            return None
    return {"since": raw.get("since"), "until": until, "reason": raw.get("reason")}
