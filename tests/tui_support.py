"""Shared helpers for the TUI test modules (flat tests/test_tui_*.py files import from here)."""
from __future__ import annotations

import asyncio
import importlib
import os as _os
import pkgutil
from pathlib import Path

import maestro.tui as maestro_tui
from textual.screen import Screen

from maestro import claims, store
from maestro.sessions import DryRunSessions
from maestro.tui import MaestroTUI, _FILTERS


def _make_app(home):
    dry = DryRunSessions()
    app = MaestroTUI(home=str(home), sessions_factory=lambda cfg: dry)
    app.dry = dry
    return app


def _filter_idx(name: str) -> int:
    return next(i for i, (n, _) in enumerate(_FILTERS) if n == name)


# BINDINGS entries can be plain tuples or Binding dataclass instances.
def _bkey(b) -> str:
    return b.key if hasattr(b, "key") else b[0]


def _baction(b) -> str:
    return b.action if hasattr(b, "action") else b[1]


# --------------------------------------------------------------------------- #
# (c) open each modal / screen via its action, escape to dismiss              #
# --------------------------------------------------------------------------- #

async def _open_via_action(app, pilot, action, expect_type):
    before = len(app.screen_stack)
    await app.run_action(action)
    await pilot.pause()
    assert app._exception is None, f"{action} crashed: {app._exception!r}"
    top = app.screen_stack[-1]
    assert isinstance(top, expect_type), \
        f"{action} -> {type(top).__name__}, want {expect_type.__name__}"
    assert len(app.screen_stack) == before + 1
    await pilot.press("escape")
    await pilot.pause()
    assert len(app.screen_stack) == before, f"{action} screen did not dismiss"
    assert app._exception is None


def _run_modal_test(seeded_home, selected_key, action, expect_type):
    async def _inner():
        app = _make_app(seeded_home)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            if selected_key is not None:
                app._selected_key = selected_key
            await _open_via_action(app, pilot, action, expect_type)

    asyncio.run(_inner())


# --------------------------------------------------------------------------- #
# T-153: human input nudges a key-scoped sweep and toasts the outcome          #
# --------------------------------------------------------------------------- #

def _capture_toasts(app):
    toasts: list[str] = []
    real = app.notify

    def _notify(message, *a, **kw):
        toasts.append(str(message))
        return real(message, *a, **kw)

    app.notify = _notify
    return toasts


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


# T-155: ticket actions target the visible ticket                              #
# --------------------------------------------------------------------------- #

def _spy_notify(app) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    real = app.notify

    def _notify(message, *a, severity="information", **kw):
        seen.append((str(message), severity))
        return real(message, *a, severity=severity, **kw)

    app.notify = _notify
    return seen


def _claim(home, key, tmp_path, *, pid=None, kind=None, with_log=True):
    log = tmp_path / f"{key}.log"
    log.write_text("x")
    claims.write_claim(home, key, pid if pid is not None else _os.getpid(), "t",
                       log_path=str(log) if with_log else None, kind=kind)
    return log


async def _wait_for(pilot, cond, tries=60):
    for _ in range(tries):
        await pilot.pause(0.1)
        if cond():
            return True
    return False


def _tree_bytes(home: Path, *members: str) -> dict:
    return {str(p.relative_to(home)): p.read_bytes()
            for m in members for p in sorted((home / m).rglob("*")) if p.is_file()}


# T-158: b hops to blockers/dependents, O opens the PR                         #
# --------------------------------------------------------------------------- #

def _set_deps(home, key, deps):
    spec = store.spec_path(home, key)
    spec.write_text(spec.read_text().replace(f"# {key}\n", f"# {key}\n\ndependsOn: [{', '.join(deps)}]\n", 1))


def _discover_binding_classes() -> list[type]:
    """Every maestro.tui Screen/ModalScreen subclass that defines BINDINGS, plus
    MaestroTUI, sorted by name -- a new screen or modal joins the binding checks
    the day it exists, with no hand-maintained list to forget."""
    for info in pkgutil.walk_packages(maestro_tui.__path__, "maestro.tui."):
        importlib.import_module(info.name)

    def _walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from _walk(sub)

    found = {c for c in _walk(Screen)
             if c.__module__.startswith("maestro.tui") and "BINDINGS" in vars(c)}
    found.add(MaestroTUI)
    return sorted(found, key=lambda c: c.__qualname__)


_BINDING_CLASSES = _discover_binding_classes()
