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


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "daemon" / "install.sh"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def up(home: Path, interval: int = 300, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    p = run([str(script), "up", "--interval", str(interval)],
            env=env, capture_output=True, text=True)
    return {"action": "up", "interval": interval, "rc": p.returncode,
            "stdout": (p.stdout or "").strip()}


def down(home: Path, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    p = run([str(script), "down"], env=env, capture_output=True, text=True)
    return {"action": "down", "rc": p.returncode, "stdout": (p.stdout or "").strip()}


def _interval_from_plist(plist=None) -> int | None:
    plist = Path(plist) if plist else _plist_path()
    if not plist.exists():
        return None
    m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>",
                  plist.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else None


def status(home: Path, *, run=subprocess.run, plist=None) -> dict:
    try:
        p = run(["launchctl", "list"], capture_output=True, text=True)
        loaded = p.returncode == 0 and LABEL in (p.stdout or "")
    except FileNotFoundError:
        loaded = False
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(store.now_epoch() - hb["epoch"]) if hb.get("epoch") else None
    return {"loaded": loaded, "heartbeat_age_s": age,
            "interval": _interval_from_plist(plist), "label": LABEL}
