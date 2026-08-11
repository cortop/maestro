"""maestro.runner_permissions -- the opencode permission block generator (T-34/RF-5).
Built from the SAME predicate source as the Claude hook / argv adapter shim, so these
tests assert it stays that way: sourced from destructive_command_guard, denies the
path-qualified bypass the PATH shim can't catch, and never constructs `--auto`.
"""
import re
from pathlib import Path

from maestro import runner_permissions


def test_denies_bare_and_path_qualified_forms_for_every_protected_subtree(home):
    perms = runner_permissions.opencode_bash_permissions(home)
    home = home.resolve()
    for rel in ("", "events", "tickets", "inbox", "config.toml"):
        target = str(home / rel) if rel else str(home)
        bare = [p for p, action in perms.items()
                if action == "deny" and f"rm *{target}" in p and "/rm" not in p]
        qualified = [p for p, action in perms.items()
                     if action == "deny" and f"/rm *{target}" in p]
        assert bare, f"missing a bare `rm` deny pattern for {target!r}"
        assert qualified, f"missing a path-qualified (/bin/rm-style) deny pattern for {target!r}"


def test_verbs_are_read_from_the_core_module_not_hand_copied(home):
    core = runner_permissions._load_core()
    core_verbs = set(runner_permissions._risky_verbs(core))
    assert core_verbs == {"rm", "mv", "truncate"}
    perms = runner_permissions.opencode_bash_permissions(home)
    for verb in core_verbs:
        assert perms.get(verb) == "ask"  # bare verb, no path -- can't statically resolve cwd


def test_never_denies_a_bare_wildcard_everything():
    """A deny-block that's too broad is as dangerous as too narrow -- it must never
    swallow legitimate work by denying "*"."""
    perms = runner_permissions.opencode_bash_permissions(Path("/tmp/not-a-real-home"))
    assert perms.get("*") != "deny"


def test_missing_core_module_raises_instead_of_silently_duplicating(monkeypatch, home):
    monkeypatch.setattr(runner_permissions, "_CORE_RELATIVE_PATH",
                         Path(".claude/hooks/does-not-exist.py"))
    try:
        runner_permissions.opencode_bash_permissions(home)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError when the core module is missing")


_MAESTRO_SRC = Path(__file__).resolve().parents[1] / "maestro"


def test_no_argv_maestro_constructs_ever_carries_the_literal_dangerous_auto_flag():
    """opencode's `--auto` ("auto-approve permissions that are not explicitly
    denied (dangerous!)") INVERTS the permission model this ticket exists to make
    safe -- it must never appear anywhere maestro builds an argv list (today: only
    `sessions.py`'s ClaudeCliSessions.spawn, but this sweeps the whole package so a
    future non-Claude backend can't reintroduce it unnoticed either)."""
    hits = []
    for path in _MAESTRO_SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "--auto" in text:
            hits.append(str(path.relative_to(_MAESTRO_SRC.parent)))
    assert not hits, f"literal '--auto' found in: {hits}"
