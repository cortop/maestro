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

from . import snapshot as snap_mod
from . import store
from .statemachine import Phase

_FRONTMATTER_FIELD_RE = re.compile(r"^([a-zA-Z_]\w*)\s*:\s*(.+)$")


def parse_spec_overrides(spec_text: str) -> dict:
    """Extract optional kind/model/effort/repo/runner/runner_model/approval_tier from a
    spec's loose frontmatter. Stops at the first ## section header. Returns only keys
    that are present. ``approval_tier`` is parsed to int; a malformed value (not an
    int) is simply omitted -- callers fall back to the safe, more-restrictive
    default (see ``spec_tier``) rather than this function ever raising. RF-2:
    ``runner``/``runner_model`` follow ``model``/``effort``'s precedent exactly --
    returned verbatim, no normalisation, no validation (that's
    ``dispatcher.resolve_runner``'s job, and the fail-closed-on-unregistered-name
    check is the dispatcher's, not this function's).
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
        elif field == "approval_tier":
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
