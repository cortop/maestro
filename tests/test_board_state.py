"""T-99: a missing/uninitialized/partial home must be distinguishable from a
healthy empty board at every surface (`status`/`doctor`/`env`/`create`/
`dispatch`/`project`) -- never inferred from a ticket count. Every test drives
the real CLI / real `dispatch()` / real `health.report()` over a temp home,
never a mock of the classifier itself.
"""
import io
import json
import sys

import pytest

from maestro import backup, dispatcher as disp, event_log, health, projection, store
from maestro.cli import main
from maestro.config import Config, load
from maestro.sessions import DryRunSessions


def _run(home, *argv):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        code = main(["--home", str(home), *argv])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return code, buf_out.getvalue(), buf_err.getvalue()


def _run_json(home, *argv):
    code, out, err = _run(home, *argv)
    return code, json.loads(out), err


# --- AC1: store.board_state -------------------------------------------------

def test_board_state_missing_for_a_nonexistent_path(tmp_path):
    result = store.board_state(tmp_path / "nope")
    assert result == {"state": "missing",
                       "missing_paths": ["events/", "tickets/", "inbox/", "config.toml"]}


def test_board_state_uninitialized_for_an_empty_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = store.board_state(empty)
    assert result["state"] == "uninitialized"
    assert set(result["missing_paths"]) == {"events/", "tickets/", "inbox/", "config.toml"}


def test_board_state_partial_when_some_core_paths_exist(tmp_path):
    half = tmp_path / "half"
    (half / "events").mkdir(parents=True)
    (half / "tickets").mkdir(parents=True)
    result = store.board_state(half)
    assert result["state"] == "partial"
    assert set(result["missing_paths"]) == {"inbox/", "config.toml"}


def test_board_state_ok_after_a_real_backup_restore_roundtrip(tmp_path):
    """AC1: a `backup` -> `restore` round-trip home classifies "ok"."""
    import shutil

    home = tmp_path / "home"
    _run(home, "init")
    _run(home, "create", "seed", "--no-nudge")
    cfg = load(str(home))
    disp.dispatch(cfg, DryRunSessions(), now=1000)  # mints T-1
    _run(home, "backup")

    for d in ("events", "tickets", "inbox", "derived"):
        shutil.rmtree(home / d, ignore_errors=True)
    (home / "config.toml").unlink()
    assert store.board_state(home)["state"] == "uninitialized"

    backup.restore_backup(cfg)
    assert store.board_state(home)["state"] == "ok"


def test_board_state_ok_for_a_fresh_init_with_zero_tickets(tmp_path):
    """AC1: `init` classifies "ok" even with no tickets -- the whole point is a
    structural fingerprint, never a ticket count."""
    home = tmp_path / "home"
    _run(home, "init")
    result = store.board_state(home)
    assert result == {"state": "ok", "missing_paths": []}


def test_find_did_you_mean_prefers_a_child_ok_board(tmp_path):
    """AC3: the `~/.maestro` vs `~/.maestro/maestro-dev` trap -- the live board
    sits one directory below a bare, never-`init`ed default home."""
    parent = tmp_path / "dotmaestro"
    parent.mkdir()
    child = parent / "maestro-dev"
    _run(child, "init")
    assert store.find_did_you_mean(parent) == child


def test_find_did_you_mean_none_when_nothing_nearby_is_ok(tmp_path):
    assert store.find_did_you_mean(tmp_path / "nope") is None


# --- AC1/AC3: `maestro status` ----------------------------------------------

def test_status_ok_board_exits_0_with_home_and_board_first(tmp_path):
    home = tmp_path / "home"
    _run(home, "init")
    code, out, _ = _run_json(home, "status")
    keys = list(out.keys())
    assert keys[:2] == ["home", "board"]
    assert out["board"] == {"state": "ok", "missing_paths": []}
    assert code == 0


def test_status_missing_home_exits_2_with_stderr_remedy(tmp_path):
    home = tmp_path / "nope"
    code, out, err = _run_json(home, "status")
    assert code == 2
    assert out["board"]["state"] == "missing"
    assert str(home) in err
    assert "maestro" in err and "init" in err


def test_status_missing_home_names_a_sibling_ok_board_as_did_you_mean(tmp_path):
    """The exact incident shape: a bare default home with a real board one
    directory below it."""
    dotmaestro = tmp_path / ".maestro"
    live = dotmaestro / "maestro-dev"
    _run(live, "init")

    code, out, err = _run_json(dotmaestro, "status")
    assert code == 2
    assert out["board"]["state"] == "uninitialized"
    assert str(live) in err


def test_status_partial_board_exits_1_with_missing_paths(tmp_path):
    home = tmp_path / "home"
    (home / "events").mkdir(parents=True)
    (home / "tickets").mkdir(parents=True)
    (home / "inbox").mkdir(parents=True)
    # config.toml deliberately absent -> partial
    code, out, _ = _run_json(home, "status")
    assert code == 1
    assert out["board"]["state"] == "partial"
    assert out["board"]["missing_paths"] == ["config.toml"]


# --- AC6: `maestro create` ---------------------------------------------------

def test_create_against_missing_home_exits_2_and_creates_nothing(tmp_path):
    home = tmp_path / "nope"
    code, _, err = _run(home, "create", "a ticket", "--no-nudge")
    assert code == 2
    assert "init" in err
    assert not home.exists()


def test_create_against_uninitialized_home_exits_2_and_creates_nothing(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    code, _, err = _run(home, "create", "a ticket", "--no-nudge")
    assert code == 2
    assert list(home.iterdir()) == []


def test_create_against_an_ok_board_is_unaffected(tmp_path):
    home = tmp_path / "home"
    _run(home, "init")
    code, out, _ = _run(home, "create", "a ticket", "--no-nudge")
    assert code == 0
    assert "queued create" in out


# --- AC4/AC5: health.check_home_structure + doctor exit code ----------------

def test_check_home_structure_is_first_in_checks_registry():
    assert health.CHECKS[0] is health.check_home_structure


def test_doctor_fails_closed_on_a_missing_home(tmp_path):
    home = tmp_path / "nope"
    cfg = Config(home=home)
    rpt = health.report(cfg, 1000)
    assert rpt["home"] == str(home)
    assert rpt["board"]["state"] == "missing"
    assert [c["name"] for c in rpt["checks"]] == ["home_structure"]
    assert rpt["checks"][0]["status"] == "fail"

    code, out, _ = _run_json(home, "doctor")
    assert code == 2 or code == 1  # cmd_doctor's own exit, checked precisely below
    assert out["checks"][0]["status"] == "fail"


def test_doctor_cli_exits_nonzero_on_a_missing_home(tmp_path):
    home = tmp_path / "nope"
    code, out, _ = _run_json(home, "doctor")
    assert code == 1
    assert out["board"]["state"] == "missing"


def test_doctor_warns_on_a_partial_home_but_stays_exit_0_by_default(tmp_path):
    home = tmp_path / "home"
    (home / "events").mkdir(parents=True)
    (home / "tickets").mkdir(parents=True)
    (home / "inbox").mkdir(parents=True)
    code, out, _ = _run_json(home, "doctor")
    home_check = next(c for c in out["checks"] if c["name"] == "home_structure")
    assert home_check["status"] == "warn"
    assert "config.toml" in home_check["detail"]
    assert code == 0


def test_doctor_strict_still_catches_a_partial_home(tmp_path):
    home = tmp_path / "home"
    (home / "events").mkdir(parents=True)
    (home / "tickets").mkdir(parents=True)
    (home / "inbox").mkdir(parents=True)
    code, _, _ = _run(home, "doctor", "--strict")
    assert code == 1


def test_doctor_ok_board_home_structure_check_is_ok(home, cfg):
    result = health.check_home_structure(cfg, 1000)
    assert result == {"name": "home_structure", "status": "ok",
                       "detail": "home structure intact", "state": "ok", "missing_paths": []}


# --- AC8: doctor against a nonexistent home leaves the filesystem untouched --

def test_doctor_against_nonexistent_home_writes_no_files(tmp_path):
    home = tmp_path / "does-not-exist"
    _run(home, "doctor")
    assert not home.exists()


# --- AC7: `maestro env` ------------------------------------------------------

def test_env_keyless_includes_board_and_still_exits_0(tmp_path):
    home = tmp_path / "home"
    _run(home, "init")
    code, out, _ = _run_json(home, "env")
    assert code == 0
    assert out["board"] == {"state": "ok", "missing_paths": []}
    assert out["home"] == str(home)


def test_env_keyless_reports_missing_board_but_still_exits_0(tmp_path):
    """Reconciler preambles call `env` (keyless) unconditionally -- it must
    never itself start refusing on a broken home."""
    home = tmp_path / "nope"
    code, out, _ = _run_json(home, "env")
    assert code == 0
    assert out["board"]["state"] == "missing"


# --- AC9: dispatch refuses an unrestricted sweep over a tickets/-less home ---

def _seed_phantom_events(home, key="T-1"):
    """events/ populated, but no tickets/ dir at all -- Repro 4's shape."""
    (home / "events").mkdir(parents=True, exist_ok=True)
    (home / "inbox").mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[maestro]\n")
    event_log.append(home, key, "Failed", {"error": "no spec.md"}, actor="reconciler")
    event_log.append(home, key, "PhaseChanged", {"phase": "triaging"}, actor="reconciler")


def test_dispatch_refuses_an_unrestricted_sweep_when_tickets_dir_is_missing(tmp_path):
    home = tmp_path / "home"
    _seed_phantom_events(home)
    assert not (home / "tickets").exists()
    cfg = Config(home=home)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)
    assert report.spawned == []  # dry_run's own would-spawn slot
    assert report.due == []

    real_report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert real_report.spawned == []
    assert real_report.due == []

    records = store.read_jsonl(disp.dispatch_ledger_path(home))
    assert records[-1]["decisions"]["_board"]["outcome"] == "board_refused"


def test_dispatch_key_filter_sweep_is_unaffected_by_the_board_refusal(tmp_path):
    """RB-17 AC4: `maestro cmd <KEY> discard` (a `--key` sweep) must keep
    working end to end even over a `tickets/`-less home."""
    home = tmp_path / "home"
    _seed_phantom_events(home, key="T-1")
    from maestro import snapshot as snap_mod
    snap_mod.rebuild(home, "T-1")
    cfg = Config(home=home)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, key_filter=["T-1"])
    # Not refused -- the phantom key itself is still due-checked normally
    # (it has real event-log history, so `_never_minted` is False).
    assert report.due or report.spawned or report.claimed


# --- AC10: `maestro project` warns on a non-ok board ------------------------

def test_project_stamps_a_warning_banner_on_a_partial_board(tmp_path):
    home = tmp_path / "home"
    (home / "events").mkdir(parents=True)
    (home / "tickets").mkdir(parents=True)
    (home / "inbox").mkdir(parents=True)
    out = projection.render(home)
    assert "Board state: partial" in out["WORKSTATE.md"]
    assert "config.toml" in out["WORKSTATE.md"]


def test_project_no_banner_on_an_ok_board(tmp_path):
    home = tmp_path / "home"
    _run(home, "init")
    out = projection.render(home)
    assert "Board state" not in out["WORKSTATE.md"]
