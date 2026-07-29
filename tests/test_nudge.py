"""Tests for the event-driven nudge: ans/cmd/create trigger an in-process dispatch."""
import pytest

from maestro import cli, dispatcher as disp, event_log, inbox, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _seed_awaiting_human(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: 0\n")
    event_log.append(
        home, key, "TicketCreated",
        {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d",
    )
    event_log.append(
        home, key, "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r",
    )
    event_log.append(
        home, key, "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r",
    )
    snap_mod.rebuild(home, key)


def _dispatch_calls(monkeypatch):
    """Capture disp.dispatch calls; replace with no-op that records them."""
    calls = []

    def fake_dispatch(cfg, sessions, now):
        calls.append({"cfg": cfg, "sessions": sessions, "now": now})
        return disp.DispatchReport(
            minted=[], due=[], claimed=[], spawned=[], capacity_skipped=[], active_sessions=0
        )

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    return calls


def test_ans_triggers_nudge_dispatch(home, monkeypatch):
    """maestro ans must trigger an in-process dispatch sweep."""
    _seed_awaiting_human(home, "T-1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert len(calls) == 1


def test_no_nudge_flag_suppresses_dispatch(home, monkeypatch):
    """--no-nudge prevents the in-process dispatch sweep."""
    _seed_awaiting_human(home, "T-1")
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "ans", "T-1", "yes", "--no-nudge"])
    assert rc == 0
    assert calls == []


def test_config_nudge_false_suppresses_dispatch(home, monkeypatch):
    """nudge_on_human_input=false in config suppresses nudge even without --no-nudge."""
    _seed_awaiting_human(home, "T-1")
    calls = _dispatch_calls(monkeypatch)

    # Write a config with nudge disabled
    (home / "config.toml").write_text("[maestro]\nnudge_on_human_input = false\n")
    rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert calls == []


def test_nudge_does_not_double_spawn_live_claim(home, monkeypatch):
    """If a reconciler is already live (claim exists), the nudge dispatch skips it."""
    _seed_awaiting_human(home, "T-1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})

    sessions = DryRunSessions(active={"T-1"})
    monkeypatch.setattr(cli, "ClaudeCliSessions", lambda *a, **kw: sessions)

    rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    # Dispatch ran but T-1 was already claimed — no second spawn
    assert sessions.spawned == []


def test_cmd_triggers_nudge(home, monkeypatch):
    """maestro cmd also nudges dispatch."""
    _seed_awaiting_human(home, "T-1")
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "cmd", "T-1", "retry"])
    assert rc == 0
    assert len(calls) == 1


def test_cmd_no_nudge_suppresses_dispatch(home, monkeypatch):
    """maestro cmd --no-nudge suppresses dispatch."""
    _seed_awaiting_human(home, "T-1")
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "cmd", "T-1", "retry", "--no-nudge"])
    assert rc == 0
    assert calls == []


def test_create_triggers_nudge(home, monkeypatch):
    """maestro create nudges dispatch so the new ticket is minted immediately."""
    (home / "tickets").mkdir(parents=True, exist_ok=True)
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "create", "My new ticket"])
    assert rc == 0
    assert len(calls) == 1


def test_create_no_nudge_suppresses_dispatch(home, monkeypatch):
    """maestro create --no-nudge suppresses dispatch."""
    (home / "tickets").mkdir(parents=True, exist_ok=True)
    calls = _dispatch_calls(monkeypatch)

    rc = cli.main(["--home", str(home), "create", "My new ticket", "--no-nudge"])
    assert rc == 0
    assert calls == []


def test_ans_on_paused_fleet_queues_and_prints_notice(home, monkeypatch, capsys):
    """AC: on a paused home, `maestro ans` exits 0, leaves the answer pending in
    the inbox, spawns nothing, and prints a paused notice — driven through the
    REAL disp.dispatch (only the external claude-spawn boundary is substituted)."""
    from maestro import fleet

    _seed_awaiting_human(home, "T-1")
    sessions = DryRunSessions()
    monkeypatch.setattr(cli, "ClaudeCliSessions", lambda *a, **kw: sessions)
    fleet.pause(home, reason="mid-flight")

    rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert "fleet is paused" in capsys.readouterr().out
    assert sessions.spawned == []
    # The answer is durably queued, not lost.
    assert inbox.pending(home, "T-1") != []

    # A real sweep after resume picks it up.
    fleet.resume(home)
    report = disp.dispatch(Config(home=home), sessions, now=store.now_epoch())
    assert "T-1" in report.spawned
