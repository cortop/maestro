"""T-125 (c): permanent tripwire -- no `maestro.*` module (outside the optional
`tui` extra) may import a model runtime at module-load time. The stdlib-only
core (CLAUDE.md: "Keep the core dependency-free") is a design invariant this
turns into a test instead of a code-review convention, per the Laya
evaluation's conclusion that any model must live behind an optional extra and
a sidecar, never the reconciler core.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys

import maestro

# The exact set the Laya spike flagged: a heavy ML/model-runtime dependency
# that has no business being importable from the deterministic core.
_FORBIDDEN_MODULES = ("torch", "transformers", "laya", "numpy")


def _core_module_names() -> list[str]:
    """Every `maestro.*` submodule except the `tui` extra (needs `textual`,
    an intentionally optional dependency, not a model runtime, but still out
    of scope for the stdlib-only core this tripwire guards)."""
    names = []
    for info in pkgutil.walk_packages(maestro.__path__, prefix="maestro."):
        if info.name == "maestro.tui" or info.name.startswith("maestro.tui."):
            continue
        names.append(info.name)
    return names


def test_no_model_runtime_imported():
    names = _core_module_names()
    assert names, "walk_packages found no maestro.* submodules -- test is not exercising anything"
    for name in names:
        importlib.import_module(name)
    leaked = [mod for mod in _FORBIDDEN_MODULES if mod in sys.modules]
    assert not leaked, (
        f"importing every non-tui maestro.* module pulled in a model runtime: {leaked} -- "
        "the stdlib-only core must never import one (see CLAUDE.md)"
    )
