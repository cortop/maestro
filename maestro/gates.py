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


def spec_runner(home: Path, key: str) -> tuple[str | None, str | None]:
    """*key*'s `runner:`/`runner_model:` spec front-matter overrides, read
    straight from the spec file on disk -- mirrors `spec_tier`/`spec_priority`
    exactly, so a human's `maestro runner` edit (or the TUI's runner modal,
    UX-2) is visible to display surfaces the very next render, without waiting
    for a fold. Missing file or missing field both come back `None` -- there is
    no numeric-style safe default for a runner name, so unlike `spec_tier` this
    does not default to `"claude"`; a display caller treats `None` as "board
    default" (`cfg.runner`), not as an error."""
    spec_file = store.spec_path(home, key)
    if not spec_file.exists():
        return None, None
    overrides = parse_spec_overrides(spec_file.read_text(encoding="utf-8"))
    return overrides.get("runner"), overrides.get("runner_model")


def runner_editable(home: Path, key: str, snap: snap_mod.Snapshot) -> bool:
    """True iff *key*'s spec `runner:`/`runner_model:` front-matter may still
    be rewritten through ``ops.set_runner`` (UX-1). THE RULE MUST HAVE EXACTLY
    ONE DEFINITION -- this is it, called by both the CLI ``runner`` verb and
    the TUI's runner modal (both funnel through ``ops.set_runner``, so there is
    only ever one call site of this predicate).

    Before T-54, a runner choice only ever took effect at the `implementing`
    spawn, so the editable window was a fixed phase whitelist (``triaging``/
    ``awaiting-human``/``ready``) plus "no worktree yet" as a defensive second
    check for the narrow race where `ready`'s own reconciler has already
    called `worktree ensure` and is about to transition to `implementing`
    without a fresh spawn in between. Phase-aware routing (T-54) breaks that
    whitelist: a runner can now be admitted to `qa`/`researching`/`triaging`
    too (`dispatcher.runner_eligible_phases`), and a ticket can cycle back
    through `implementing` more than once (a QA fix round), so no fixed set of
    "pre-implementation" phases is still correct -- a worktree can exist, and
    `implementing` can have already happened, while a FUTURE runner-bearing
    spawn (the next fix round, or the next phase admitted for this runner) is
    still ahead of it.

    The one condition that generalizes correctly, because every runner-bearing
    spawn (regardless of which phase it turns out to be for) shares it: "a
    running session's spawn args -- runner included -- were frozen at launch
    time" is true exactly while a session for *key* is actually in flight, and
    false otherwise. So: editable unless *key* has a CONFIRMED-live claim right
    now (``claims.verify_claim`` -- never ``claims.is_claimed``, which
    *releases* a stale claim as a side effect; a guard/display predicate must
    never mutate state). No live session -> whatever spawn comes next (any
    phase) hasn't happened yet -> editable. A live session -> its spawn args
    are already frozen -> refuse, a hard gate rather than a warning, since a
    late edit would otherwise silently apply only to the session *after* this
    one.

    *snap* is accepted (not read) purely for call-site/signature parity with
    every other gate predicate here (`needs_approval` et al) -- every caller
    already has a fresh snapshot in hand from its own `snap_mod.load`.
    """
    del snap
    return claims.verify_claim(home, key) != "confirmed"


# T-34/RF-5: implementer providers this dispatcher trusts to already carry a
# bypass-resistant equivalent of the destructive-command guard. Claude Code's
# PreToolUse hook (`.claude/hooks/block-home-deletion.py` + `guard_argv.py`,
# both driven by `destructive_command_guard.check_command`) is the only one
# today. Adding a provider name here IS the "wiring" step for a future backend
# (e.g. opencode) -- it must not happen by accident just because a provider
# name was typed into config.toml.
#
# T-59/PI-7: "pi" joins the set as the LAST commit of that ticket, once (and
# only once) every other AC was green -- `maestro.pi_guard` wires a
# bypass-resistant guard extension (sourced from the SAME
# `destructive_command_guard.check_command` predicate, plus pi-only widening
# for the reconciler's own worktree and the guard's own install directory,
# the human-only maestro-verb denylist, and force-push), proven with a REAL
# `pi` run in `tests/test_pi_guard.py` (not just a unit test of the
# predicate) -- see that ticket's spec for the full bar this had to clear
# before landing here.
BYPASS_RESISTANT_IMPLEMENTERS = {"claude_skill", "pi"}


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
