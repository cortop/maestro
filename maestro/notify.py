"""Outbound push notifications on phase transitions the human should see.

Fires from the dispatcher sweep (exact sync/backup/schedule pattern: cursor-gated,
idempotent, no global lock) so it works with no TUI open — a question sitting in
``awaiting-human`` otherwise goes unseen until someone happens to look. Diffs each
key's phase against a persisted ``derived/.notify_cursor.json`` cursor and fires at
most once per key per entry into a watched phase -- or into the tier-2 approval
gate (``gates.needs_approval``), which is not a phase, so the cursor tracks a
composite (phase, gated) state instead of phase alone (see ``_notify_state``).

Import-cycle note (GA-21): ``dispatcher.py`` imports this module at load time, so
this module must never import ``dispatcher`` back at ITS load time -- only the
``list_keys`` call below needs it, and that import stays function-local/lazy.
``gates.py`` is safe to import at the top: it imports neither ``dispatcher`` nor
this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import gates, snapshot as snap_mod, store
from .config import Config
from .statemachine import Phase

WATCHED_PHASES = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value, Phase.DONE.value}
_TIMEOUT = 10  # seconds; a hung command/webhook must never stall the sweep


def _cursor_path(home: Path) -> Path:
    return home / "derived" / ".notify_cursor.json"


def _question_text(snap: snap_mod.Snapshot) -> str:
    return next(iter(snap.open_questions.values()), "") if snap.open_questions else ""


def _notify_state(phase: str, gated: bool) -> str:
    """Cursor value for one key. When *gated* is False this is exactly the old
    cursor format (a bare phase string) -- an already-deployed board's cursor
    file keeps comparing correctly for the vast majority of keys that never
    hit the approval gate. Only a gated key gets the (new) suffixed form, so
    entering the gate always reads as a state change even though `phase` alone
    (still "implementing") did not move."""
    return f"{phase}:gated" if gated else phase


def _run_command(cmd: str, key: str, phase: str, question: str) -> None:
    env = dict(os.environ, KEY=key, PHASE=phase, QUESTION=question)
    try:
        subprocess.run(cmd, shell=True, env=env, timeout=_TIMEOUT,
                        capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # a broken/missing notify_command must never abort the sweep


def _post_webhook(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=_TIMEOUT).close()
    except (urllib.error.URLError, OSError, ValueError):
        pass  # an unreachable webhook must never abort the sweep


def maybe_notify(cfg: Config, now: float) -> list[str]:
    """Dispatcher hook: fire ``notify_command``/``webhook_urls`` at most once per
    key on first entry into awaiting-human/degraded/done, or into the tier-2
    approval gate (``gates.needs_approval`` -- not a phase, so it can't change
    `snap.phase` the way the other three do; tracked via the composite cursor
    state instead, see ``_notify_state``). No-op when neither notify_command nor
    webhook_urls is configured. Returns the keys that fired this sweep."""
    if not cfg.notify_command and not cfg.webhook_urls:
        return []
    from . import dispatcher as disp_mod  # lazy: dispatcher imports notify at top level too

    home = cfg.home
    cursor_path = _cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    fired: list[str] = []
    changed = False

    for key in disp_mod.list_keys(home):
        snap = snap_mod.load(home, key)
        gated = gates.needs_approval(home, key, snap)
        state = _notify_state(snap.phase, gated)
        if state != cursor.get(key):
            cursor[key] = state
            changed = True
            if snap.phase in WATCHED_PHASES or gated:
                question = _question_text(snap)
                if cfg.notify_command:
                    _run_command(cfg.notify_command, key, snap.phase, question)
                for url in cfg.webhook_urls:
                    _post_webhook(url, {
                        "key": key, "phase": snap.phase, "gated": gated,
                        "question": question, "title": snap.title,
                    })
                fired.append(key)

    if changed:
        store.write_json(cursor_path, cursor)
    return fired
