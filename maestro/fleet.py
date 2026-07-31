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

import hashlib
import importlib.resources
import os
import re
import subprocess
from pathlib import Path

from . import store

# The legacy, pre-multi-home label. Still used verbatim for the DEFAULT home
# (``~/.maestro``) so existing single-home installs are not orphaned by this
# change -- only a NON-default home gets a slugged label (see ``label()``).
LEGACY_LABEL = "com.maestro.dispatcher"

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


def _default_home() -> Path:
    return (Path.home() / ".maestro").resolve()


def _slug(home: Path) -> str:
    """Sanitized basename + short hash of the resolved home path, so two homes
    that happen to share a basename (``~/.maestro`` vs. ``/other/.maestro``)
    still get distinct labels."""
    resolved = str(home)
    basename = re.sub(r"[^A-Za-z0-9]+", "-", home.name).strip("-").lower() or "home"
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:8]
    return f"{basename}-{digest}"


def label(home: Path) -> str:
    """launchd label for *home*'s dispatcher. The default ``~/.maestro`` home
    keeps the bare legacy label (existing installs are not orphaned); any other
    home gets ``com.maestro.dispatcher.<slug>`` so two homes never collide."""
    resolved = Path(home).expanduser().resolve()
    if resolved == _default_home():
        return LEGACY_LABEL
    return f"{LEGACY_LABEL}.{_slug(resolved)}"


def _legacy_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"


def _plist_home(plist: Path) -> str | None:
    """The ``MAESTRO_HOME`` baked into an on-disk plist, if any."""
    if not plist.exists():
        return None
    m = re.search(r"<key>MAESTRO_HOME</key>\s*<string>(.*?)</string>",
                  plist.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _same_home(a, b) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def _plist_path(home: Path | None = None) -> Path:
    lbl = label(home) if home is not None else LEGACY_LABEL
    return Path.home() / "Library" / "LaunchAgents" / f"{lbl}.plist"


def clamp_interval(interval) -> int:
    """Coerce a requested cadence to a sane integer at or above ``MIN_INTERVAL``."""
    try:
        val = int(interval)
    except (TypeError, ValueError):
        return MIN_INTERVAL
    return max(MIN_INTERVAL, val)


def _migrate_legacy_plist(home: Path, lbl: str, *, run) -> dict | None:
    """If *home* now gets a slugged label, unload+remove a same-home legacy
    plist so ``up()`` doesn't leave two jobs pointed at one home. A legacy
    plist baked for a DIFFERENT home is left untouched -- it belongs to that
    other home's own migration -- and we just warn about it."""
    if lbl == LEGACY_LABEL:
        return None
    legacy = _legacy_plist_path()
    if not legacy.exists():
        return None
    baked_home = _plist_home(legacy)
    if baked_home and _same_home(baked_home, home):
        run(["launchctl", "unload", str(legacy)], capture_output=True, text=True)
        legacy.unlink(missing_ok=True)
        return {"migrated_legacy": True, "legacy_plist": str(legacy)}
    return {"migrated_legacy": False,
            "warning": f"legacy plist {legacy} is bound to a different home "
                       f"({baked_home!r}); left untouched"}


def up(home: Path, interval: int = 300, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    requested = interval
    interval = clamp_interval(interval)
    lbl = label(home)
    migration = _migrate_legacy_plist(home, lbl, run=run)
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    env["MAESTRO_LABEL"] = lbl
    p = run([str(script), "up", "--interval", str(interval)],
            env=env, capture_output=True, text=True)
    out = {"action": "up", "interval": interval, "rc": p.returncode,
           "stdout": (p.stdout or "").strip(), "label": lbl}
    if requested != interval:
        out["clamped_from"] = requested
    if migration:
        out.update(migration)
    return out


def down(home: Path, *, run=subprocess.run, script=None) -> dict:
    script = Path(script) if script else _script_path()
    lbl = label(home)
    env = dict(os.environ)
    env["MAESTRO_HOME"] = str(home)
    env["MAESTRO_LABEL"] = lbl
    p = run([str(script), "down"], env=env, capture_output=True, text=True)
    out = {"action": "down", "rc": p.returncode, "stdout": (p.stdout or "").strip(),
           "label": lbl}
    # Clean up a same-home legacy plist left over from before this home ever
    # migrated (i.e. `down` run without a prior `up` under the new label).
    if lbl != LEGACY_LABEL:
        legacy = _legacy_plist_path()
        baked_home = _plist_home(legacy)
        if legacy.exists() and baked_home and _same_home(baked_home, home):
            run(["launchctl", "unload", str(legacy)], capture_output=True, text=True)
            legacy.unlink(missing_ok=True)
            out["migrated_legacy"] = True
    return out


def _plist_integer(text: str, key: str) -> int | None:
    m = re.search(rf"<key>{key}</key>\s*<integer>(\d+)</integer>", text)
    return int(m.group(1)) if m else None


def _interval_from_plist(plist=None, *, home=None) -> int | None:
    plist = Path(plist) if plist else _plist_path(home)
    if not plist.exists():
        return None
    return _plist_integer(plist.read_text(encoding="utf-8"), "StartInterval")


def _throttle_from_plist(plist=None, *, home=None) -> int | None:
    plist = Path(plist) if plist else _plist_path(home)
    if not plist.exists():
        return None
    return _plist_integer(plist.read_text(encoding="utf-8"), "ThrottleInterval")


_LAST_EXIT_RE = re.compile(r'"LastExitStatus"\s*=\s*(-?\d+);')


def last_exit_code(home: Path | None = None, *, run=subprocess.run) -> int | None:
    """Parse ``LastExitStatus`` from ``launchctl list <LABEL>`` (a per-job query,
    unlike the bare ``launchctl list`` ``status()`` uses to check membership) --
    ``None`` if the job isn't loaded or ``launchctl`` isn't available. *home*
    resolves which home's (possibly slugged) label to query; omitted means the
    legacy default label."""
    lbl = label(home) if home is not None else LEGACY_LABEL
    try:
        p = run(["launchctl", "list", lbl], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if p.returncode != 0:
        return None
    m = _LAST_EXIT_RE.search(p.stdout or "")
    return int(m.group(1)) if m else None


def _label_column(line: str) -> str:
    # launchctl list's bare form is tab-separated: PID, Status, Label.
    cols = line.split("\t")
    return cols[-1].strip() if cols else ""


def _any_label_loaded(labels: set[str], output: str) -> bool:
    lines = (output or "").splitlines()
    return any(_label_column(line) in labels for line in lines)


def status(home: Path, *, run=subprocess.run, plist=None) -> dict:
    lbl = label(home)
    labels_to_check = {lbl}
    # During the transition, a home may still be registered under the legacy
    # label if it hasn't run `fleet up` since upgrading -- check both, but
    # only the legacy job actually baked for THIS home (never another home's).
    if lbl != LEGACY_LABEL:
        legacy = _legacy_plist_path()
        baked_home = _plist_home(legacy)
        if baked_home and _same_home(baked_home, home):
            labels_to_check.add(LEGACY_LABEL)
    try:
        p = run(["launchctl", "list"], capture_output=True, text=True)
        loaded = p.returncode == 0 and _any_label_loaded(labels_to_check, p.stdout or "")
    except FileNotFoundError:
        loaded = False
    hb = store.read_json(home / "derived" / ".heartbeat.json", {})
    age = round(store.now_epoch() - hb["epoch"]) if hb.get("epoch") else None
    pause = pause_state(home, store.now_epoch())
    return {"loaded": loaded, "heartbeat_age_s": age,
            "interval": _interval_from_plist(plist, home=home),
            "throttle": _throttle_from_plist(plist, home=home), "label": lbl,
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
