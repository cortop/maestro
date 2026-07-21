"""`maestro fleet` — manage the launchd dispatcher LaunchAgent.

Thin, testable wrapper over ``daemon/install.sh`` plus a status probe. Each function
accepts an injectable ``run`` (defaults to ``subprocess.run``) so the behaviour can be
tested against a fake ``launchctl`` / install script without touching the real system.
"""
from __future__ import annotations

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
    return Path(__file__).resolve().parents[1] / "daemon" / "install.sh"


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
    return {"loaded": loaded, "heartbeat_age_s": age,
            "interval": _interval_from_plist(plist),
            "throttle": _throttle_from_plist(plist), "label": LABEL}
