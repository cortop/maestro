"""Human actions (answer, message, retry, discard, hold, release, stop, pause, resume) shared by the TUI, CLI and web UI."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import dispatcher as disp
from . import fleet, inbox, ops, store
from . import snapshot as snap_mod
from .config import Config
from .statemachine import Phase

# Called with the ticket key (or None) after a successful inbox write; None = no nudge.
Nudge = Callable[[str | None], object]


class StopRefused(store.MaestroError):
    """`stop` could not stop the session; ``result`` is the `ops.stop_session` dict."""

    def __init__(self, result: dict):
        super().__init__(str(result.get("outcome") or "session not stopped"))
        self.result = result


def _fire(nudge: Nudge | None, key: str | None) -> None:
    if nudge is not None:
        nudge(key)


def _require_minted(home: Path, key: str) -> None:
    if disp._never_minted(home, key):
        raise store.MaestroError(f"unknown ticket {key!r}")


def answer(home: Path, key: str, text: str, *, qid: str | None = None,
           accepted_recommendation: bool = False, nudge: Nudge | None = None) -> dict:
    """Queue an `ans` for *key*; ``accepted_recommendation`` marks a recommendation accept."""
    args: dict = {"text": text}
    if qid:
        args["qid"] = qid
    if accepted_recommendation:
        args["accepted_recommendation"] = True
    entry = inbox.append_command(home, key, "ans", args)
    _fire(nudge, key)
    return entry


def message(home: Path, key: str, text: str, *, nudge: Nudge | None = None) -> dict:
    """Queue a free-text `msg` for *key*'s agent."""
    entry = inbox.append_command(home, key, "msg", {"text": text})
    _fire(nudge, key)
    return entry


def _degraded_only(home: Path, key: str, command: str, nudge: Nudge | None) -> dict:
    if snap_mod.load(home, key).phase != Phase.DEGRADED.value:
        raise store.MaestroError(f"{key}: '{command}' only applies to degraded tickets")
    entry = inbox.append_command(home, key, command, {})
    _fire(nudge, key)
    return entry


def retry(home: Path, key: str, *, nudge: Nudge | None = None) -> dict:
    """Queue `retry` for a degraded ticket; refuses any other phase."""
    return _degraded_only(home, key, "retry", nudge)


def discard(home: Path, key: str, *, nudge: Nudge | None = None) -> dict:
    """Queue `discard` for a degraded ticket; refuses any other phase."""
    return _degraded_only(home, key, "discard", nudge)


def hold(home: Path, key: str, *, until: float | None = None, reason: str | None = None) -> dict:
    """Hold *key* so the dispatcher never spawns it; refuses an unknown ticket."""
    _require_minted(home, key)
    return fleet.hold(home, key, until=until, reason=reason)


def release(home: Path, key: str) -> dict:
    """Release *key*'s hold; refuses an unknown ticket with no hold file."""
    if not fleet.hold_path(home, key).exists():
        _require_minted(home, key)
    return fleet.unhold(home, key)


def stop(cfg: Config, key: str, *, wait: float = 10.0) -> dict:
    """SIGTERM *key*'s live session; raises `StopRefused` unless the process stopped."""
    result = ops.stop_session(cfg, key, wait=wait)
    if not result["stopped"]:
        raise StopRefused(result)
    return result


def pause(home: Path, *, until: float | None = None, reason: str | None = None) -> dict:
    """Pause the whole fleet."""
    return fleet.pause(home, until=until, reason=reason)


def resume(home: Path) -> dict:
    """Resume a paused fleet."""
    return fleet.resume(home)
