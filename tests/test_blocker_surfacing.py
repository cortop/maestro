"""T-118: the dispatcher's silent skips reach a human.

Roughly 96% of dispatch decisions append nothing anywhere -- every skip path
deliberately writes no event, which is individually correct (don't burn an
attempts slot over a 30-second blip) and collectively fatal, because you
cannot detect a non-event by reading a log of events. `_write_heartbeat` had
parameters for `repo_blockers` and silently dropped `runner_blockers`, which
`dispatch()` already computed and returned on its report; the only other
consumer was dispatch stdout. A board sat frozen for hours behind 26 OK / 2
WARN.

Every test drives the real surface: a real `dispatch(cfg, DryRunSessions(),
...)` sweep or the real CLI over a temp home, then asserts on the heartbeat
and the rendered NEEDS-YOU.md.
"""
import json

from maestro import cli, dispatcher as disp, event_log, projection, \
    snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.IMPLEMENTING, runner=None):
    spec = f"# {key}\n"
    if runner:
        spec += f"runner: {runner}\nrunner_model: glm-5.2\n"
    spec += "\n## Acceptance criteria\n- [ ] ok\n"
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


def _heartbeat(home):
    return store.read_json(store.heartbeat_path(home), {})


def _pi_cfg(home, monkeypatch):
    """A config that will actually route `implementing` to pi.

    `_RUNNER_DEFAULT_PHASES["pi"]` is deliberately EMPTY (PI-8), so without an
    explicit `phases` the ticket resolves straight back to claude, spawns, and
    never reaches the preflight this file is about.
    """
    from maestro import config as config_mod
    (home / "config.toml").write_text(
        '[maestro]\nrunner_enabled = ["claude", "pi"]\n'
        '[runner.pi]\nphases = ["implementing"]\n', encoding="utf-8")
    monkeypatch.setattr(disp, "_REGISTERED_RUNNERS", frozenset({"claude", "pi"}))
    return config_mod.load(str(home))


def _blocked_sweep(home, cfg):
    """A real sweep whose only mocked boundary is the runner's own probe."""
    return disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch(),
                         runner_probe=lambda runner: {"binary_ok": False, "models": None,
                                                      "daemon_reason": None})


# --- the constant itself -----------------------------------------------------

def test_silent_skip_outcomes_excludes_the_healthy_and_the_already_parked():
    """A section that cries wolf gets ignored, which is the failure mode this
    whole change exists to fix."""
    s = disp._SILENT_SKIP_OUTCOMES
    for healthy in ("not_due", "claimed", "throttled", "capacity_skipped",
                    "runner_capped", "spawned", "would_spawn"):
        assert healthy not in s, healthy
    # These already land in NEEDS-YOU's `## Questions` via _ask_park.
    for parked in ("runner_disabled", "runner_model_unavailable"):
        assert parked not in s, parked
    assert "runner_binary_missing" in s and "repo_blocked" in s


def test_blocked_keys_filters_a_real_decisions_map():
    decisions = {
        "A-1": {"outcome": "not_due", "reason": "terminal"},
        "A-2": {"outcome": "runner_binary_missing", "reason": "active"},
        "A-3": {"outcome": "throttled", "reason": "timer"},
        "A-4": {"outcome": "repo_blocked", "reason": "active"},
    }
    assert disp.blocked_keys(decisions) == {
        "A-2": "runner_binary_missing", "A-4": "repo_blocked"}


# --- a real sweep records the blocker ----------------------------------------

def test_a_real_sweep_records_runner_blockers_and_blocked_keys(home, cfg, monkeypatch):
    _seed(home, "PI-1", runner="pi")
    _blocked_sweep(home, _pi_cfg(home, monkeypatch))
    hb = _heartbeat(home)
    assert hb["blocked"] == {"PI-1": "runner_binary_missing"}
    assert "pi" in hb["runner_blockers"]
    assert "not found on PATH" in hb["runner_blockers"]["pi"]


def test_the_blocker_reaches_needs_you(home, cfg, monkeypatch):
    _seed(home, "PI-1", runner="pi")
    _blocked_sweep(home, _pi_cfg(home, monkeypatch))
    projection.write(home)
    text = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")

    assert "## Blocked — maestro cannot act" in text
    assert "runner `pi`" in text
    assert "PI-1" in text
    assert "[runner.pi] bin" in text
    # The headline bug must not survive its own fix.
    assert "Nothing is waiting on you" not in text
    # Ordering: Blocked sits above Questions.
    if "## Questions" in text:
        assert text.index("## Blocked") < text.index("## Questions")


def test_a_clean_board_still_says_nothing_is_waiting(home, cfg):
    _seed(home, "OK-1", phase=Phase.READY)
    disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())
    projection.write(home)
    text = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")
    assert "Nothing is waiting on you" in text
    assert "## Blocked" not in text


# --- the dry-run trap --------------------------------------------------------

def test_make_dry_does_not_erase_a_blocker_a_real_sweep_recorded(home, cfg, monkeypatch):
    """`cmd_dispatch` calls `projection.write` unconditionally, and a --dry-run
    sweep skips the runner preflight entirely -- so a naive fix would let
    `make dry` blank the Blocked section the last real sweep wrote."""
    _seed(home, "PI-1", runner="pi")
    _blocked_sweep(home, _pi_cfg(home, monkeypatch))
    assert _heartbeat(home)["blocked"] == {"PI-1": "runner_binary_missing"}

    rc = cli.main(["--home", str(home), "dispatch", "--dry-run"])
    assert rc == 0

    hb = _heartbeat(home)
    assert hb["blocked"] == {"PI-1": "runner_binary_missing"}, "dry-run erased it"
    assert "pi" in hb["runner_blockers"], "dry-run erased the runner blocker"
    text = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")
    assert "## Blocked — maestro cannot act" in text


def test_a_paused_board_does_not_erase_a_recorded_blocker(home, cfg, monkeypatch):
    """The fleet-pause kill switch writes the heartbeat and returns before the
    blocker locals exist."""
    _seed(home, "PI-1", runner="pi")
    armed = _pi_cfg(home, monkeypatch)
    _blocked_sweep(home, armed)

    from maestro import fleet
    fleet.pause(home)
    disp.dispatch(armed, DryRunSessions(), now=store.now_epoch())

    hb = _heartbeat(home)
    assert hb["paused"] is True
    assert hb["blocked"] == {"PI-1": "runner_binary_missing"}, "pause erased it"


# --- hook errors -------------------------------------------------------------

def test_a_hook_error_surfaces_as_a_blocker(home, cfg, monkeypatch):
    """The live board carried `sync_external_sources: HTTP 401` on ~every sweep
    with nothing anywhere a human looks."""
    _seed(home, "OK-1", phase=Phase.READY)

    def boom(*a, **k):
        raise RuntimeError("HTTP Error 401: Unauthorized")
    monkeypatch.setattr(disp, "sync_external_sources", boom)
    disp.dispatch(cfg, DryRunSessions(), now=store.now_epoch())

    hb = _heartbeat(home)
    assert "sync_external_sources" in hb["hook_errors"]
    projection.write(home)
    text = (home / "derived" / "NEEDS-YOU.md").read_text(encoding="utf-8")
    assert "hook `sync_external_sources`" in text
    assert "401" in text
