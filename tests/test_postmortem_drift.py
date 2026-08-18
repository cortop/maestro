"""Drift guard for docs/postmortem-2026-07-19.md (T-92): the doc is a dated,
hand-written record (not a generated derivation like docs/state-machine.md), so
there is nothing to regenerate it against -- but the one thing that would rot
silently and be worse than no document at all is a cited control symbol or
config key that no longer resolves in the package. This mirrors
tests/test_diagram.py's drift-guard posture (fail `make test` the moment a
cited/generated artifact drifts from its source) without trying to regenerate
the prose itself."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from maestro import alarm, backup, config as config_mod, dispatcher, health, ops, ratelimit, sessions, spend

REPO_ROOT = Path(__file__).resolve().parents[1]
POSTMORTEM_PATH = REPO_ROOT / "docs" / "postmortem-2026-07-19.md"

# Every maestro module the postmortem cites a `module.symbol` control against,
# keyed by the exact prefix used in the doc's backticked citations.
_MODULES = {
    "dispatcher": dispatcher,
    "health": health,
    "spend": spend,
    "ratelimit": ratelimit,
    "alarm": alarm,
    "backup": backup,
    "ops": ops,
    "sessions": sessions,
}

_SYMBOL_RE = re.compile(r"`(" + "|".join(re.escape(m) for m in _MODULES) + r")\.([A-Za-z_][A-Za-z0-9_]*)")
_CONFIG_RE = re.compile(r"`cfg\.([A-Za-z_][A-Za-z0-9_]*)")

# `module.py` is a file-path citation (e.g. `` `maestro/dispatcher.py:431` `` or the
# bare `` `dispatcher.py` ``), not a symbol -- "py" is never a real attribute name, so
# it's excluded rather than requiring every file mention in the doc's prose to carry
# the `maestro/` prefix.
_NOT_A_SYMBOL = {"py"}


def test_postmortem_doc_exists():
    assert POSTMORTEM_PATH.exists(), "docs/postmortem-2026-07-19.md is missing"


def test_postmortem_cited_symbols_still_resolve():
    text = POSTMORTEM_PATH.read_text(encoding="utf-8")
    found = [(mod, attr) for mod, attr in _SYMBOL_RE.findall(text) if attr not in _NOT_A_SYMBOL]
    assert found, "no backticked `module.symbol` control citations found in the postmortem"
    missing = [f"{mod}.{attr}" for mod, attr in found if not hasattr(_MODULES[mod], attr)]
    assert not missing, (
        "docs/postmortem-2026-07-19.md cites symbols that no longer resolve in the "
        f"package -- update the doc or restore the symbol: {missing}")


def test_postmortem_cited_config_fields_still_resolve():
    text = POSTMORTEM_PATH.read_text(encoding="utf-8")
    found = _CONFIG_RE.findall(text)
    assert found, "no backticked `cfg.<field>` citations found in the postmortem"
    field_names = {f.name for f in dataclasses.fields(config_mod.Config)}
    missing = sorted(set(found) - field_names)
    assert not missing, (
        "docs/postmortem-2026-07-19.md cites cfg.<field> names that are no longer "
        f"Config fields -- update the doc or restore the field: {missing}")
