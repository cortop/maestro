"""OC-3: board-wide `runner_enabled` kill switch -- consulted in the spawn loop
BEFORE any of OC-2's own preflight (before the binary probe, before the daemon
probe). A runner name absent from the list is a config problem, not a
transient one, so it gets the same PERMANENT `ops.ask` treatment
`runner_unregistered`/`runner_model_unavailable` already use: no attempts-
ledger spend, a stable qid, parked in awaiting-human.

`_REGISTERED_RUNNERS` (RF-2) is still `{"claude"}` -- no real second
`SessionManager` delegate exists yet -- so, exactly like
`test_runner_preflight.py`, every test here monkeypatches it to admit a fake
non-claude runner name past that earlier gate and into this ticket's new
`runner_enabled` gate.

## Notes
The first real bring-up (spec AC4) ran against a **scratch** MAESTRO_HOME --
`/tmp/maestro-oc3-brief-obTcDC` -- never `~/.maestro/maestro-dev`: `maestro
--home <scratch> init` + `maestro --home <scratch> env` printed the effective
default (`"runner_enabled": ["claude"]`) for real; a hand-seeded `BAD-1`
ticket with `runner: opencode` in `implementing` then got a real (non-dry-run)
`maestro --home <scratch> dispatch` -- `spawned: []`, and `BAD-1` moved to
`awaiting-human` (the fail-closed artifact), confirmed via `maestro --home
<scratch> env --key BAD-1`. The scratch dir was removed after.
"""
from __future__ import annotations

import io
import json
import sys

from maestro import cli, config as config_mod, dispatcher as disp, event_log, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_runner_preflight import RUNNER, _counting_probe, _register, _seed

# --- AC1: knob at its default -- zero spawns, same fail-closed artifact OC-2's
# own permanent branch defines (ops.ask, stable qid, awaiting-human, no
# attempts spend), probe never even called -----------------------------------


def test_default_knob_blocks_a_disabled_runner_before_any_probe(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    assert cfg.runner_enabled == ["claude"]  # default: claude only
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned
    assert probe.calls == []  # never reaches OC-2's own preflight

    events = event_log.read(home, "BAD-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert asked[0]["payload"]["qid"] == "runner-disabled-BAD-1-opencode"
    assert not any(e["type"] == "Failed" for e in events)

    from maestro import snapshot as snap_mod
    snap = snap_mod.load(home, "BAD-1")
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert snap.failure_count == 0

    attempts = store.read_json(home / "derived" / ".spawn_attempts.json", {}) or {}
    assert "BAD-1" not in attempts


def test_default_knob_never_spawns_the_disabled_runner_under_claude(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    sessions = DryRunSessions()

    disp.dispatch(cfg, sessions, now=1000, runner_probe=_counting_probe(
        {"binary_ok": True, "models": [], "daemon_reason": None}))

    assert "BAD-1" not in {s[0] for s in sessions.spawned}


def test_second_sweep_does_not_duplicate_the_question(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    probe = _counting_probe({"binary_ok": True, "models": [], "daemon_reason": None})

    disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)
    disp.dispatch(cfg, DryRunSessions(), now=1001, runner_probe=probe)

    events = event_log.read(home, "BAD-1")
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert probe.calls == []


# --- AC2: knob enabling the runner -- the sweep proceeds to OC-2's own
# preflight (asserted by the probe being called) -----------------------------


def test_enabling_the_runner_lets_the_sweep_reach_oc2_preflight(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    cfg.runner_enabled = ["claude", RUNNER]
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)

    assert "BAD-1" not in report.spawned  # still blocked -- daemon unreachable now
    assert probe.calls == [RUNNER]  # but the probe WAS reached this time
    assert RUNNER in report.runner_blockers


# --- AC3: flipping the knob requires no spec edit anywhere -------------------


def test_flipping_the_knob_touches_no_spec(home, cfg, monkeypatch):
    _register(monkeypatch, RUNNER)
    _seed(home, "BAD-1", phase=Phase.IMPLEMENTING, runner=RUNNER, runner_model="a:1b")
    spec_path = store.spec_path(home, "BAD-1")
    before = spec_path.read_bytes()
    probe = _counting_probe({"binary_ok": True, "models": None, "daemon_reason": "refused"})

    disp.dispatch(cfg, DryRunSessions(), now=1000, runner_probe=probe)
    after_disabled = spec_path.read_bytes()

    cfg.runner_enabled = ["claude", RUNNER]  # flip the knob -- config only
    disp.dispatch(cfg, DryRunSessions(), now=1001, runner_probe=probe)
    after_enabled = spec_path.read_bytes()

    assert before == after_disabled == after_enabled


# --- config.load(): scalar or list, default claude-only ----------------------


def test_config_load_default_is_claude_only(tmp_path):
    cfg = config_mod.load(str(tmp_path))
    assert cfg.runner_enabled == ["claude"]


def test_config_load_accepts_a_scalar(tmp_path):
    (tmp_path / "config.toml").write_text('[maestro]\nrunner_enabled = "opencode"\n')
    cfg = config_mod.load(str(tmp_path))
    assert cfg.runner_enabled == ["opencode"]


def test_config_load_accepts_a_list(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[maestro]\nrunner_enabled = ["claude", "opencode"]\n')
    cfg = config_mod.load(str(tmp_path))
    assert cfg.runner_enabled == ["claude", "opencode"]


def test_cmd_init_does_not_write_runner_enabled_into_an_existing_home(tmp_path):
    # cmd_init only writes DEFAULT_CONFIG_TOML when config.toml is absent --
    # an existing home (with no [maestro] runner_enabled line) stays untouched
    # and still resolves to the claude-only default via config.load().
    (tmp_path / "config.toml").write_text("[maestro]\nmax_concurrency = 7\n")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        cli.main(["--home", str(tmp_path), "init"])
    finally:
        sys.stdout = old
    text = (tmp_path / "config.toml").read_text()
    assert "runner_enabled" not in text
    cfg = config_mod.load(str(tmp_path))
    assert cfg.runner_enabled == ["claude"]


# --- AC1 (env clause): real `maestro env` prints the effective value ---------


def test_real_maestro_env_prints_the_effective_runner_enabled(tmp_path):
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(["--home", str(tmp_path), "env"])
    finally:
        sys.stdout = old
    assert code == 0
    out = json.loads(buf.getvalue())
    assert out["runner_enabled"] == ["claude"]
