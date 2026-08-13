"""The tier-2 approval gate: one predicate, read from the spec + the snapshot.

Split out of ``dispatcher.py`` (GA-21) so every human-facing surface — not just
the dispatcher's own ``is_due`` — can call the exact same ``needs_approval``
function instead of re-deriving the rule (which is how NEEDS-YOU.md, ``maestro
status``, the TUI, and outbound notifications all went blind to a gated ticket
in the first place). This module sits BELOW ``dispatcher`` in the import graph
on purpose: ``dispatcher.py`` imports ``notify`` at module load time, so
``notify.py`` can never import ``dispatcher`` back without a cycle -- but it can
import this module directly, since ``gates`` imports neither ``dispatcher`` nor
``notify``. ``dispatcher`` re-exports ``spec_tier``/``parse_spec_overrides`` from
here so existing ``from .dispatcher import spec_tier`` callers (``repos.py``,
``projection.py``, ``tui/app.py``, ``tui/screens.py``) keep working unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import claims
from . import snapshot as snap_mod
from . import store
from .config import Config
from .statemachine import Phase

_FRONTMATTER_FIELD_RE = re.compile(r"^([a-zA-Z_]\w*)\s*:\s*(.+)$")


def parse_spec_overrides(spec_text: str) -> dict:
    """Extract optional kind/model/effort/repo/runner/runner_model/approval_tier/
    priority from a spec's loose frontmatter. Stops at the first ## section
    header. Returns only keys that are present. ``approval_tier``/``priority``
    are parsed to int; a malformed value (not an int) is simply omitted --
    callers fall back to the safe default (see ``spec_tier``/``spec_priority``)
    rather than this function ever raising. RF-2: ``runner``/``runner_model``
    follow ``model``/``effort``'s precedent exactly -- returned verbatim, no
    normalisation, no validation (that's ``dispatcher.resolve_runner``'s job,
    and the fail-closed-on-unregistered-name check is the dispatcher's, not
    this function's).
    """
    result: dict = {}
    for line in spec_text.splitlines():
        if line.startswith("##"):
            break
        m = _FRONTMATTER_FIELD_RE.match(line)
        if not m:
            continue
        field, val = m.group(1), m.group(2).strip()
        if field in ("kind", "model", "effort", "repo", "runner", "runner_model"):
            result[field] = val
        elif field in ("approval_tier", "priority"):
            try:
                result[field] = int(val)
            except ValueError:
                pass
    return result


def spec_tier(home: Path, key: str) -> int:
    """*key*'s approval tier, read straight from the spec file on disk (not the
    snapshot, so a human edit takes effect the very next sweep). Missing file,
    missing field, or a malformed value all fall back to 1 -- the same
    more-restrictive default used at mint (``args.get("approval_tier") or 1``)
    -- so ``is_due``/spawn-arg construction can stay total and never wedge on a
    bad spec."""
    spec_file = store.spec_path(home, key)
    if not spec_file.exists():
        return 1
    overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))
    return overrides.get("approval_tier", 1)


def spec_priority(home: Path, key: str) -> int:
    """*key*'s dispatch-ordering preference, read straight from the spec file on
    disk (not the snapshot, so a human edit takes effect the very next sweep) --
    mirrors ``spec_tier`` exactly. Missing file, missing field, or a malformed
    value all fall back to the default (3), the same default ``_seed_spec``
    already coerces to, so a sweep can never raise on a bad/absent value
    (MTO-7). Lower sorts first -- see ``dispatcher.dispatch``'s ordering of the
    due set, upstream of every existing throttle/cap; this function is only a
    read, it decides nothing about whether a key is due or spawnable."""
    spec_file = store.spec_path(home, key)
    if not spec_file.exists():
        return 3
    overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))
    return overrides.get("priority", 3)


def needs_approval(home: Path, key: str, snap: snap_mod.Snapshot) -> bool:
    """True iff *key* is parked at the tier-2 ``implementing`` approval gate
    (AD-1): ``phase == implementing and spec_tier(home, key) >= 2 and not
    snap.approved``. THE RULE MUST HAVE EXACTLY ONE DEFINITION -- this is it.
    ``dispatcher.is_due`` calls this directly (so ``not_due``'s "needs-approval"
    reason and this function can never drift apart), and every human-facing
    surface (NEEDS-YOU.md, ``maestro status``, the TUI filter/toast/command
    modal, outbound notify) calls it too, instead of re-deriving the tier/
    approved check inline."""
    return (Phase(snap.phase) == Phase.IMPLEMENTING
            and spec_tier(home, key) >= 2
            and not snap.approved)


# UX-1: phases where a ticket's `runner:`/`runner_model:` choice may still be
# changed -- before a worktree/reconciler for the `implementing` step exists,
# so an edit can't be silently ignored by a session already spawned.
_RUNNER_EDITABLE_PHASES = frozenset({Phase.TRIAGING, Phase.AWAITING_HUMAN, Phase.READY})


def runner_editable(home: Path, key: str, snap: snap_mod.Snapshot) -> bool:
    """True iff *key*'s spec `runner:`/`runner_model:` front-matter may still
    be rewritten through ``ops.set_runner`` (UX-1). THE RULE MUST HAVE EXACTLY
    ONE DEFINITION -- this is it, called by both the CLI ``runner`` verb and
    (later) the TUI's runner modal.

    Three conditions, all required: *key* is still in ``triaging``/
    ``awaiting-human``/``ready`` (``implementing``, or anything past it, means
    a reconciler for that step may already exist); no worktree exists on disk
    yet (``store.worktree_path`` -- never ``ops``, so this stays a cheap,
    side-effect-free filesystem check); and its claim, if any, is not
    CONFIRMED-live (``claims.verify_claim`` -- never ``claims.is_claimed``,
    which *releases* a stale claim as a side effect; a guard/display predicate
    must never mutate state).

    A running session's spawn args -- runner included -- were frozen at
    launch time; a late edit would silently apply only to the *next* session,
    which is worse than refusing outright, so this is a hard gate, not a
    warning.
    """
    try:
        phase_ok = Phase(snap.phase) in _RUNNER_EDITABLE_PHASES
    except ValueError:
        phase_ok = False
    if not phase_ok:
        return False
    if store.worktree_path(home, key).exists():
        return False
    if claims.verify_claim(home, key) == "confirmed":
        return False
    return True


# T-34/RF-5: implementer providers this dispatcher trusts to already carry a
# bypass-resistant equivalent of the destructive-command guard. Claude Code's
# PreToolUse hook (`.claude/hooks/block-home-deletion.py` + `guard_argv.py`,
# both driven by `destructive_command_guard.check_command`) is the only one
# today. Adding a provider name here IS the "wiring" step for a future backend
# (e.g. opencode) -- it must not happen by accident just because a provider
# name was typed into config.toml.
BYPASS_RESISTANT_IMPLEMENTERS = {"claude_skill"}


def backend_interlock_reason(cfg: Config) -> str | None:
    """None if ``cfg.providers["implementer"]`` already has a bypass-resistant
    destructive-command guard wired in (see ``BYPASS_RESISTANT_IMPLEMENTERS``);
    otherwise the human-facing reason a spawn into it must be refused.

    THE interlock the T-34/RF-5 ticket Notes require: "until a bypass-resistant
    equivalent exists, a predicate makes any non-Claude backend refuse to
    spawn -- enforced by a test, not by merge order." ``dispatcher.dispatch``
    calls this once per sweep (config-only, no per-key state) and asks each
    due key rather than spawning it while a reason is returned -- see
    ``dispatcher.dispatch``'s spawn loop. Reads config only, so any other
    human-facing surface (``maestro doctor``, the TUI) can surface the same
    refusal without re-deriving it.
    """
    implementer = (cfg.providers or {}).get("implementer")
    if implementer in BYPASS_RESISTANT_IMPLEMENTERS:
        return None
    return (
        f"implementer provider {implementer!r} has no bypass-resistant "
        "destructive-command guard wired in yet (see .claude/hooks/"
        "block-home-deletion.py, guard_argv.py, and the T-34/RF-5 opencode-"
        "spike Note) -- refusing to spawn until one exists and this provider "
        "is added to gates.BYPASS_RESISTANT_IMPLEMENTERS"
    )
