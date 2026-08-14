"""QW-4: proves the autouse `_guard_user_scope_installs` fixture
(tests/conftest.py) actually fails closed, rather than merely asserting the
guard's intent -- and that `skills_install.USER_SCOPE_RESOLVERS` can't drift
from the module's own resolver functions without the drift itself failing
the suite.
"""
import inspect
from pathlib import Path

import pytest

from maestro import skills_install
from conftest import assert_user_scope_confined


def test_guard_passes_for_the_real_resolvers_under_tmp_path(tmp_path):
    """Sanity check: with the real four resolvers and the autouse fixture's
    own env-var redirection already in effect (this test runs under it like
    every other test), the guard raises nothing."""
    assert_user_scope_confined(tmp_path)


def test_guard_fails_loudly_on_an_escaping_resolver(tmp_path, monkeypatch):
    """AC3: add a temporary fifth resolver that escapes tmp_path (points at
    the real $HOME) and confirm the guard actually raises -- not that it was
    merely intended to."""
    def _escaping_resolver(cfg=None):
        return Path.home() / ".config" / "opencode" / "command"

    monkeypatch.setattr(
        skills_install, "USER_SCOPE_RESOLVERS",
        skills_install.USER_SCOPE_RESOLVERS + (_escaping_resolver,))

    with pytest.raises(AssertionError, match="_escaping_resolver"):
        assert_user_scope_confined(tmp_path)


def test_user_scope_resolvers_registry_matches_module_naming_convention():
    """Guards the guard itself: every top-level `skills_install` function
    named like a user-scope resolver (`user_commands_dir`, or any
    `opencode_user_*`) must appear in `USER_SCOPE_RESOLVERS` -- so a future
    fifth resolver that follows the same naming convention but is left out of
    the tuple fails this test instead of silently bypassing the guard."""
    discovered = {
        name for name, obj in vars(skills_install).items()
        if inspect.isfunction(obj) and obj.__module__ == skills_install.__name__
        and (name == "user_commands_dir" or name.startswith("opencode_user_"))
    }
    registered = {resolver.__name__ for resolver in skills_install.USER_SCOPE_RESOLVERS}
    assert discovered == registered
