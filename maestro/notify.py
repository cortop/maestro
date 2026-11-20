"""Outbound push notifications on phase transitions the human should see.

Fires from the dispatcher sweep (exact sync/backup/schedule pattern: cursor-gated,
idempotent, no global lock) so it works with no TUI open — a question sitting in
``awaiting-human`` otherwise goes unseen until someone happens to look. Diffs each
key's phase against a persisted ``derived/.notify_cursor.json`` cursor and fires at
most once per key per entry into a watched phase.

AD-7 removed the tier-2 ``implementing`` approval gate (``gates.needs_approval``)
this module used to also watch for -- that gate was a hidden hold inside an
active phase, not a real phase transition, so the cursor used to track a
composite (phase, gated) state instead of phase alone. Now every hold is a real
phase (``awaiting-human``), so the cursor is back to a bare phase string.

Import-cycle note (GA-21): ``dispatcher.py`` imports this module at load time, so
this module must never import ``dispatcher`` back at ITS load time -- only the
``list_keys`` call below needs it, and that import stays function-local/lazy.
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
from .review.model import is_review
from .statemachine import Phase

WATCHED_PHASES = {Phase.AWAITING_HUMAN.value, Phase.DEGRADED.value, Phase.DONE.value}
_TIMEOUT = 10  # seconds; a hung command/webhook must never stall the sweep


def _cursor_path(home: Path) -> Path:
    return home / "derived" / ".notify_cursor.json"


def _question_text(snap: snap_mod.Snapshot) -> str:
    return next(iter(snap.open_questions.values()), "") if snap.open_questions else ""


def _run_command(cmd: str, key: str, phase: str, question: str, *, kind: str = "ticket",
                 event: str | None = None, title: str = "", url: str = "",
                 requester: str = "") -> None:
    # title/requester are other people's text: environment values only, never in the command string.
    env = dict(os.environ, KEY=key, PHASE=phase, QUESTION=question, KIND=kind,
               EVENT=event if event is not None else phase, TITLE=title, URL=url,
               REQUESTER=requester)
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


_PUSH_CURSOR_KEY = "__push__"  # dedupe tokens live beside the per-key phase entries


def _deliver(cfg: Config, key: str, *, kind: str, event: str, phase: str, question: str,
             title: str, url: str, requester: str, text: str) -> None:
    if cfg.notify_command:
        _run_command(cfg.notify_command, key, phase, question or text, kind=kind,
                     event=event, title=title, url=url, requester=requester)
    for hook in cfg.webhook_urls:
        payload = {"key": key, "phase": phase, "question": question or text, "title": title,
                   "kind": kind, "event": event}
        if url:
            payload["url"] = url
        if requester:
            payload["requester"] = requester
        _post_webhook(hook, payload)


def _review_event(snap: snap_mod.Snapshot) -> str | None:
    """The event a review's phase entry maps to, or None when it pushes nothing."""
    if snap.phase == Phase.AWAITING_HUMAN.value:
        if any(q.startswith("review-brief-") for q in snap.open_questions):
            return "brief-ready"
        return "blocked"
    if snap.phase == Phase.DEGRADED.value:
        return "failed"
    return None


def push(cfg: Config, key: str, event: str, *, title: str | None = None,
         url: str | None = None, requester: str | None = None, text: str | None = None,
         dedupe: str = "") -> bool:
    """Push the named *event* for *key* to ``notify_command`` and ``webhook_urls``, once per
    (key, event, dedupe token). Returns True when it fired."""
    if not cfg.notify_command and not cfg.webhook_urls:
        return False
    home = cfg.home
    cursor_path = _cursor_path(home)
    cursor = store.read_json(cursor_path, {}) or {}
    seen = cursor.get(_PUSH_CURSOR_KEY)
    if not isinstance(seen, dict):
        seen = {}
    token = f"{key}\x00{event}\x00{dedupe}"
    if token in seen:
        return False
    seen[token] = True
    cursor[_PUSH_CURSOR_KEY] = seen
    store.write_json(cursor_path, cursor)
    snap = snap_mod.load(home, key)
    kind = "review" if is_review(snap) else "ticket"
    _deliver(cfg, key, kind=kind, event=event, phase=snap.phase, question="",
             title=title if title is not None else snap_mod.display_title(home, snap),
             url=url or "", requester=requester or "", text=text or "")
    return True


def maybe_notify(cfg: Config, now: float) -> list[str]:
    """Dispatcher hook: fire ``notify_command``/``webhook_urls`` at most once per
    key on first entry into awaiting-human/degraded/done. No-op when neither
    notify_command nor webhook_urls is configured. Returns the keys that fired
    this sweep."""
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
        if snap.phase != cursor.get(key):
            cursor[key] = snap.phase
            changed = True
            review = is_review(snap)
            event = _review_event(snap) if review else (
                snap.phase if snap.phase in WATCHED_PHASES else None)
            if event is not None:
                _deliver(cfg, key, kind="review" if review else "ticket", event=event,
                         phase=snap.phase, question=_question_text(snap), text="",
                         title=snap_mod.display_title(home, snap),
                         url=(snap.review_url or "") if review else "",
                         requester=(snap.review_author or "") if review else "")
                fired.append(key)

    if changed:
        store.write_json(cursor_path, cursor)
    return fired
