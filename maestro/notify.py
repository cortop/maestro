"""Outbound push notifications on phase transitions the human should see.

Fires from the dispatcher sweep (exact sync/backup/schedule pattern: cursor-gated,
idempotent, no global lock) so it works with no TUI open — a question sitting in
``awaiting-human`` otherwise goes unseen until someone happens to look. Diffs each
key's phase against a persisted ``derived/.notify_cursor.json`` cursor and fires at
most once per key per entry into a watched phase.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from . import snapshot as snap_mod, store
from .config import Config
from .statemachine import Phase

WATCHED_PHASES = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value, Phase.DONE.value}
_TIMEOUT = 10  # seconds; a hung command/webhook must never stall the sweep


def _cursor_path(home: Path) -> Path:
    return home / "derived" / ".notify_cursor.json"


def _question_text(snap: snap_mod.Snapshot) -> str:
    return next(iter(snap.open_questions.values()), "") if snap.open_questions else ""


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
    key on first entry into awaiting-human/degraded/done. No-op when neither is
    configured. Returns the keys that fired this sweep."""
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
        last_phase = cursor.get(key)
        if snap.phase != last_phase:
            cursor[key] = snap.phase
            changed = True
            if snap.phase in WATCHED_PHASES:
                question = _question_text(snap)
                if cfg.notify_command:
                    _run_command(cfg.notify_command, key, snap.phase, question)
                for url in cfg.webhook_urls:
                    _post_webhook(url, {
                        "key": key, "phase": snap.phase,
                        "question": question, "title": snap.title,
                    })
                fired.append(key)

    if changed:
        store.write_json(cursor_path, cursor)
    return fired
