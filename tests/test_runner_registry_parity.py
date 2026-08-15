"""T-53: PI-1 -- assert `dispatcher._REGISTERED_RUNNERS` and the two
`RoutingSessions` delegate construction sites in `cli.py` (`_nudge`,
`cmd_dispatch`) can never silently drift apart. The comment at
`cli.py:108-113` says the three must be edited together; nothing enforced
that before this ticket.

Both call sites are exercised through the real CLI (`cli.main([...])`) over a
temp home, so this reaches the real (non-dry) `RoutingSessions` construction
-- `cmd_dispatch` only builds it on the non-dry branch (`--dry-run` returns
`DryRunSessions()` instead). The only patched boundary is
`ClaudeCliSessions`/`OpencodeCliSessions` themselves (never `RoutingSessions`
-- patching that would just assert the test's own mock back at itself); the
real `RoutingSessions.__init__` still runs and its `delegates` dict is what
gets inspected.
"""
import inspect
from unittest.mock import patch

import pytest

from maestro import cli, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.sessions import ClaudeCliSessions, OpencodeCliSessions, PiCliSessions, RoutingSessions
from maestro.statemachine import Phase


def _write_config(home):
    (home / "config.toml").write_text(
        "[maestro]\nrepo_path = \"/repo/default\"\nbranch_prefix = \"maestro/\"\n"
        "min_spawn_interval = 0\n", encoding="utf-8")


def _seed_awaiting_human(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(home, key, "TicketCreated",
                      {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, key, "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, key)


class _FakeDelegate:
    """Stands in for a real backend's constructed instance -- harmless
    list_active/spawn so a dispatch sweep over the (due-ticket-free) temp home
    never shells out, while still round-tripping through the real
    `RoutingSessions.__init__` and `.delegates` dict."""

    def __init__(self, *a, **k):
        pass

    def list_active(self):
        return set()

    def spawn(self, *a, **k):
        return None


def _build_delegates_at_both_sites(home, monkeypatch):
    """Drives `_nudge` (via `ans`) and `cmd_dispatch` (via `dispatch`) -- the
    two real `RoutingSessions` construction sites -- and returns
    `(nudge_delegate_keys, dispatch_delegate_keys)`, the actual key set each
    site's real `RoutingSessions.delegates` ended up holding."""
    _write_config(home)
    _seed_awaiting_human(home, "T-1")

    captured: list[set[str]] = []
    orig_init = RoutingSessions.__init__

    def spy_init(self, delegates, home=None):
        captured.append(set(delegates))
        orig_init(self, delegates, home=home)

    monkeypatch.setattr(RoutingSessions, "__init__", spy_init)

    with patch("maestro.cli.ClaudeCliSessions", side_effect=_FakeDelegate), \
         patch("maestro.cli.OpencodeCliSessions", side_effect=_FakeDelegate), \
         patch("maestro.cli.PiCliSessions", side_effect=_FakeDelegate):
        rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
        assert rc == 0
        rc = cli.main(["--home", str(home), "dispatch"])
        assert rc == 0

    assert len(captured) == 2, \
        f"expected exactly one RoutingSessions construction per call site, got {captured}"
    return captured[0], captured[1]


def _assert_delegate_keys_match_registry(delegate_keys):
    assert delegate_keys == disp._REGISTERED_RUNNERS


# --- AC1/AC2: both real construction sites carry the registry's exact key set, and
# match each other -------------------------------------------------------------

def test_routing_sessions_delegates_match_registered_runners_at_both_sites(home, monkeypatch):
    nudge_keys, dispatch_keys = _build_delegates_at_both_sites(home, monkeypatch)

    _assert_delegate_keys_match_registry(nudge_keys)
    _assert_delegate_keys_match_registry(dispatch_keys)

    # The drift the cli.py:108-113 comment actually warns about: not just each
    # site matching the registry, but the two sites matching EACH OTHER.
    assert nudge_keys == dispatch_keys


# --- AC3: every registered delegate satisfies the SessionManager protocol surface --

def test_registered_delegate_classes_satisfy_session_manager_protocol():
    delegate_classes = {"claude": ClaudeCliSessions, "opencode": OpencodeCliSessions,
                        "pi": PiCliSessions}
    # Sanity: this mapping itself must track the registry, or the sweep below
    # would silently skip a registered runner it never heard of.
    assert set(delegate_classes) == disp._REGISTERED_RUNNERS

    required_spawn_kwargs = ("model", "effort", "disallowed_tools", "allowed_tools",
                              "env_overlay", "runner", "runner_model")
    for name, cls in delegate_classes.items():
        spawn_sig = inspect.signature(cls.spawn)
        for kw in required_spawn_kwargs:
            assert kw in spawn_sig.parameters, \
                f"{name} ({cls.__name__}).spawn is missing keyword arg {kw!r}"
            kind = spawn_sig.parameters[kw].kind
            assert kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD), \
                f"{name} ({cls.__name__}).spawn's {kw!r} must be callable as a keyword argument"

        list_active_sig = inspect.signature(cls.list_active)
        assert list(list_active_sig.parameters) == ["self"], \
            f"{name} ({cls.__name__}).list_active must take no arguments beyond self"


# --- AC4: the guard must be non-vacuous -- a fabricated registry entry must fail it --

def test_registry_parity_guard_is_non_vacuous(home, monkeypatch):
    nudge_keys, _dispatch_keys = _build_delegates_at_both_sites(home, monkeypatch)

    # A real run's delegate set is {"claude", "opencode"} -- confirm the guard
    # passes on the unmodified registry first, so the failure asserted below is
    # actually caused by the fabricated entry, not some other mismatch.
    _assert_delegate_keys_match_registry(nudge_keys)

    monkeypatch.setattr(disp, "_REGISTERED_RUNNERS",
                         frozenset({*disp._REGISTERED_RUNNERS, "fabricated-runner"}))
    with pytest.raises(AssertionError):
        _assert_delegate_keys_match_registry(nudge_keys)
