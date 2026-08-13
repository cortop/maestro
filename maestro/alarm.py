"""Fleet-wide detection-channel alarm (RB-12): the 2026-07-19 runaway ran 35 hours
before anyone noticed because nothing in the package pushed toward a human -- only
``maestro doctor``'s ``runaway`` field would have caught it, and nobody was polling
it. This module is the missing detection channel. It reuses the exact transport
``notify.py`` already has (``notify_command``/``webhook_urls``, GA-12/M-12) rather
than inventing a second one -- see ``_fire``.

Runs on every real dispatcher sweep via ``_run_hook`` (like every other tick).
Unlike ``notify.maybe_notify`` -- an advisory phase-transition ping that silently
swallows a broken command/webhook -- THIS is the fleet's one detection channel, so a
broken transport must itself be loud: ``_fire`` does NOT reuse ``notify._run_command``/
``_post_webhook`` byte-for-byte (those intentionally never raise), it uses its own
checked variants that DO raise, letting the failure propagate out of ``check()`` so
``_run_hook`` records it under ``hook_errors["alarm"]`` (surfaced in ``maestro why``/
``derived/dispatch.jsonl``) -- loud, but never fatal: the sweep still spawns normally
either way, exactly like every other hook here.

Dedup state is written BEFORE any notification is attempted (see ``check()``), so a
broken transport still durably records "this episode already tried to fire" -- it
does not spam a subprocess call every single sweep while the command stays broken,
and once the command is fixed the human simply keeps missing nothing further (the
NEXT genuinely new episode still fires normally).

Four conditions, each deduped independently in ``derived/.alarm.json`` so a 60s
level-triggered sweep doesn't re-fire every tick:

- ``brake``: the runaway auto-brake (``dispatcher._maybe_trip_runaway_brake``) armed
  a NEW pause this sweep -- edge-triggered off the caller-supplied reason string;
  GA-5's own cooldown already prevents back-to-back arms, so this needs no cooldown
  of its own.
- ``spend_warn:<fraction>``: today's spend crossed ``fraction * daily_spend_ceiling_usd``
  -- fires at most once per UTC date per configured fraction (this ticket's Q&A:
  "once a day", not the generic cooldown below -- a spend warning that re-fired
  hourly while the ceiling stayed crossed would be exactly the kind of ignorable
  noise this ticket exists to prevent).
- ``spend_hard``: ``spend.over_ceiling`` blocked THIS sweep -- edge-triggered plus
  cooldown (``alarm_cooldown_s``), so a ceiling that stays blocked for hours
  re-announces at most once per cooldown instead of going silent for the rest of
  the day.
- ``spawn_rate``: ``health.spawn_rate(...)["total"] > health.spawn_budget(cfg)`` --
  the EXACT formula ``maestro doctor`` reports as ``runaway``, never a second
  threshold, so the two can never disagree. Independent of ``brake``: it fires even
  when ``runaway_pause_cooldown`` disables the auto-brake, or while a prior arm's
  own cooldown is still suppressing a new pause.
- ``provider_availability``: ``health.check_provider_availability(cfg, now)`` reports
  something other than ``ok`` (MTO-8) -- the fleet is quietly burning spawn attempts
  against a down provider or an offline box, exactly the shape of the 2026-07-19
  incident. Computed here (not threaded in from ``dispatch()`` like
  ``runaway_reason``/``spend_ceiling_reason``) since it never gates the sweep itself
  (report-only, see that check's own docstring) -- there is nothing for the caller to
  precompute. Its own probe is already behind a primary signal, so calling it every
  sweep costs nothing extra on a healthy board.

This module changes no gate behavior -- it only reads state GA-5/GA-8/GA-11/health
already compute and pushes a notification. ``derived/.alarm.json`` is disposable,
like every other ``derived/.*`` dedup/cursor file: delete it and the alarm re-arms
cold on the next sweep, no crash, no wedge.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import notify, spend as spend_mod, store
from .config import Config

_FLEET_KEY = "fleet"  # KEY passed to notify_command/webhook -- these conditions are
                       # fleet-wide, not tied to one ticket


def _state_path(home: Path) -> Path:
    return home / "derived" / ".alarm.json"


def _utc_date(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def _run_notify_command_checked(cmd: str, key: str, phase: str, question: str) -> None:
    """Same invocation shape as ``notify._run_command`` (KEY/PHASE/QUESTION in env,
    same timeout) but does NOT swallow a failure -- raises on a non-zero exit or a
    launch error (``OSError``/``SubprocessError``), so the caller can surface it
    (AC6). ``notify._run_command`` stays untouched -- its own silent-swallow
    contract is exactly right for an advisory phase-transition ping and is still
    covered by its own tests."""
    env = dict(os.environ, KEY=key, PHASE=phase, QUESTION=question)
    result = subprocess.run(cmd, shell=True, env=env, timeout=notify._TIMEOUT,
                             capture_output=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"notify_command exited {result.returncode}: {stderr}")


def _post_webhook_checked(url: str, payload: dict) -> None:
    """Same shape as ``notify._post_webhook``, but lets a failure propagate
    instead of swallowing it -- see ``_run_notify_command_checked``."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=notify._TIMEOUT).close()


def _fire(cfg: Config, condition: str, message: str) -> list[str]:
    """Push *message* through the SAME transport notify.py already fires -- never
    a second mechanism (this ticket's Notes are explicit about that) -- via the
    checked variants above so a failure is collected rather than silently
    dropped. Attempts every configured command/webhook_url regardless of an
    earlier failure; returns the failure strings (empty on full success)."""
    phase = f"alarm:{condition}"
    errors: list[str] = []
    if cfg.notify_command:
        try:
            _run_notify_command_checked(cfg.notify_command, _FLEET_KEY, phase, message)
        except Exception as e:  # noqa: BLE001 - collected below, never swallowed silently
            errors.append(f"notify_command ({condition}): {type(e).__name__}: {e}")
    for url in cfg.webhook_urls:
        try:
            _post_webhook_checked(url, {
                "key": _FLEET_KEY, "phase": phase, "condition": condition,
                "question": message, "title": message,
            })
        except Exception as e:  # noqa: BLE001
            errors.append(f"webhook {url} ({condition}): {type(e).__name__}: {e}")
    return errors


def _edge_or_cooldown(entry: dict, active: bool, now: float, cooldown: int) -> tuple[bool, dict]:
    """Level-triggered dedup with a cooldown re-announce: fires the moment
    *active* flips False->True (a fresh episode), and again if it has stayed
    True continuously for >= *cooldown* seconds since the last firing. Clearing
    (active False) drops the ``last_fired`` marker, so the NEXT entry into
    *active* is always treated as a fresh episode rather than silently
    swallowed by a cooldown that technically hasn't elapsed yet."""
    was_active = bool(entry.get("active"))
    last_fired = entry.get("last_fired")
    fire = active and (
        not was_active
        or (isinstance(last_fired, (int, float)) and now - last_fired >= cooldown)
    )
    new_entry = {"active": active}
    if active:
        new_entry["last_fired"] = now if fire else last_fired
    return fire, new_entry


def _once_per_day(entry: dict, active: bool, today: str) -> tuple[bool, dict]:
    """Fires at most once per UTC *today* while *active* -- crossing the same
    spend-warn fraction again later the same day is not a new episode."""
    if not active:
        return False, entry
    fire = entry.get("date") != today
    return (fire, {"date": today}) if fire else (fire, entry)


def check(cfg: Config, now: float, health_mod, *,
          runaway_reason: str | None, spend_ceiling_reason: str | None) -> list[str]:
    """Dispatcher hook (route through ``_run_hook``, like every other tick).
    *health_mod* is passed in rather than imported -- ``health`` imports
    ``dispatcher`` at its own module level, so importing it here would be a
    load-time cycle (mirrors ``dispatcher._maybe_trip_runaway_brake``).
    *runaway_reason*/*spend_ceiling_reason* are the values ``dispatch()`` already
    computed this sweep -- never recomputed here, so this can't drift from what
    actually gated the sweep.

    Dedup state is computed and persisted for EVERY condition before any
    notification is attempted, then every due notification is attempted
    regardless of an earlier one failing; any failures are raised as one
    aggregated exception at the end (AC6 -- ``_run_hook`` catches it and records
    ``hook_errors["alarm"]``, loud but never fatal) -- so state never goes stale
    just because the transport is broken. Returns the condition names that fired.
    """
    if not cfg.notify_command and not cfg.webhook_urls:
        return []

    home = cfg.home
    state = store.read_json(_state_path(home), {}) or {}
    fired: list[str] = []
    to_fire: list[tuple[str, str]] = []  # (condition, message) queued for _fire
    changed = False

    def note(name: str, did_fire: bool, new_entry: dict, message: str | None = None) -> None:
        nonlocal changed
        if state.get(name) != new_entry:
            state[name] = new_entry
            changed = True
        if did_fire:
            fired.append(name)
            to_fire.append((name.split(":", 1)[0].replace("_", "-"), message))

    brake_fire, brake_entry = _edge_or_cooldown(
        state.get("brake", {}), runaway_reason is not None, now, cfg.alarm_cooldown_s)
    note("brake", brake_fire, brake_entry, runaway_reason or "runaway auto-pause armed")

    st = spend_mod.status(cfg, now)
    ceiling = st["ceiling_usd"]
    today = _utc_date(now)
    if not st["unavailable"] and ceiling is not None and st["today_usd"] is not None:
        for frac in cfg.alarm_spend_warn_fractions:
            threshold = float(frac) * float(ceiling)
            active = st["today_usd"] >= threshold
            name = f"spend_warn:{frac}"
            fire, entry = _once_per_day(state.get(name, {}), active, today)
            note(name, fire, entry, (
                f"spend warning: ${st['today_usd']:.2f} has crossed "
                f"{float(frac) * 100:.0f}% of the ${float(ceiling):.2f} daily ceiling"))

    hard_fire, hard_entry = _edge_or_cooldown(
        state.get("spend_hard", {}), spend_ceiling_reason is not None, now, cfg.alarm_cooldown_s)
    note("spend_hard", hard_fire, hard_entry, spend_ceiling_reason or "daily spend ceiling reached")

    rate = health_mod.spawn_rate(home, now)["total"]
    budget = health_mod.spawn_budget(cfg)
    runaway_now = bool(budget) and rate > budget
    rate_fire, rate_entry = _edge_or_cooldown(
        state.get("spawn_rate", {}), runaway_now, now, cfg.alarm_cooldown_s)
    note("spawn_rate", rate_fire, rate_entry, (
        f"spawn rate {rate}/hour exceeds budget {budget}/hour ({health_mod.SPAWN_RATE_UNIT})"))

    provider = health_mod.check_provider_availability(cfg, now)
    provider_fire, provider_entry = _edge_or_cooldown(
        state.get("provider_availability", {}), provider["status"] != "ok",
        now, cfg.alarm_cooldown_s)
    note("provider_availability", provider_fire, provider_entry, provider["detail"])

    if changed:
        store.write_json(_state_path(home), state)  # persisted BEFORE firing -- see docstring

    errors: list[str] = []
    for condition, message in to_fire:
        errors.extend(_fire(cfg, condition, message))
    if errors:
        raise RuntimeError("; ".join(errors))
    return fired
