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


# Optional-extra packages: they need textual / fastapi + jinja2, so the core walk skips them.
_EXTRA_PACKAGES = ("tui", "web")


def _core_module_names() -> list[str]:
    """Every `maestro.*` submodule except the `tui` extra (needs `textual`,
    an intentionally optional dependency, not a model runtime, but still out
    of scope for the stdlib-only core this tripwire guards)."""
    names = []
    for info in pkgutil.walk_packages(maestro.__path__, prefix="maestro."):
        if info.name.split(".")[1] in _EXTRA_PACKAGES:
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


def _web_imports(tree, top_level_only: bool) -> list[str]:
    """Import statements in *tree* that reach the web extra (absolute or package-relative)."""
    import ast

    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found = []
    for node in nodes:
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names = [base] + [f"{base}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            bare = name.lstrip(".")
            if bare.split(".")[0] in ("fastapi", "jinja2", "uvicorn") or bare.split(".")[:2] in (
                    ["maestro", "web"], ["web"]) or bare.startswith("web."):
                found.append(name)
    return found


def test_core_never_imports_the_web_extra():
    """The `web` extra (fastapi, jinja2, uvicorn, `maestro.web`) is optional: no core module
    -- everything outside `tui/` and `web/` -- may import it at module load; only `cli.py`
    reaches it, through the lazy import inside `cmd_web`."""
    import ast
    from pathlib import Path

    root = Path(maestro.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in _EXTRA_PACKAGES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _web_imports(tree, top_level_only=str(rel) == "cli.py"):
            offenders.append(f"{rel}: {name}")
    assert not offenders, f"core modules import the optional web extra: {offenders}"
