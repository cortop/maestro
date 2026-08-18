"""RF-7: the adversarial QA loop moves out of the implementer's own session
into the dispatcher-driven `qa` phase. `implementing` hands off with
`set-phase qa` instead of spawning an in-session `Agent`-tool QA sub-agent;
`qa` (RF-6's routing, wired live here) judges the diff and routes onward --
`awaiting-ci` on an all-pass verdict, back to `implementing` on any spec-axis
fail. The round trip is bounded by the SAME `max_impl_turns` counter as
before (`ops.record_impl_turn`); no second counter is introduced.
"""
from __future__ import annotations

from maestro import cli, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from test_dispatcher import _EphemeralSessions

SPEC_TEMPLATE = """\
# {key}: {title}

approval_tier: 1
priority: 2

## Intent
{intent}

## Acceptance criteria
{acs}
"""


def _write_spec(home, key, *, title="Test ticket", intent="do the thing",
                acs=("build the widget",)):
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       SPEC_TEMPLATE.format(key=key, title=title, intent=intent, acs=ac_lines))


def _create(cfg, key, **spec_kwargs):
    _write_spec(cfg.home, key, **spec_kwargs)
    event_log.append(cfg.home, key, "TicketCreated",
                     {"title": spec_kwargs.get("title", "Test ticket"), "source": "test"}, actor="d")
    return snap_mod.rebuild(cfg.home, key)


# ---------------------------------------------------------------------------
# AC1/AC2: implementing -> qa(fail) -> implementing -> qa(pass) -> awaiting-ci,
# entirely through the real CLI and real dispatcher sweeps (routing proven at
# each hop); two distinct AcQaVerdict events for the same AC land at two
# different observed_seq values.
# ---------------------------------------------------------------------------

def test_full_roundtrip_implementing_qa_fail_implementing_qa_pass_awaiting_ci(home, cfg):
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    # -- implementing (fresh pass): self-review, PR open, hand off to qa --
    rc = cli.main(["--home", str(home), "verify-ac", "T-1", "--ac", "1", "--what", "ran pytest",
                   "--where", "tests/test_widget.py", "--result", "PASSED"])
    assert rc == 0
    event_log.append(home, "T-1", "PrOpened", {"number": 1, "url": "https://x/pull/1", "draft": True},
                     actor="reconciler", step_id="pr-T-1")
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "qa", "--requeue", "300"])
    assert rc == 0
    assert snap_mod.load(home, "T-1").phase == Phase.QA.value

    # A real sweep resolves and would spawn the independent `qa` reconciler,
    # Edit/Write denied -- proves the hand-off actually routes, not just that
    # the phase field changed.
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=2000)
    assert report.spawned == ["T-1"]
    _key, prompt, _cwd, _model, _effort, disallowed, *_ = sessions.spawned[0]
    assert prompt.startswith("/maestro-reconcile-qa T-1")
    assert "Edit" in disallowed and "Write" in disallowed

    # -- qa round 1: FAIL, routes back to implementing --
    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1", "--verdict", "fail",
                   "--evidence", "diff never exports widget.py's public function"])
    assert rc == 0
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "implementing",
                   "--reason", "qa: build the widget failed: missing export"])
    assert rc == 0
    assert snap_mod.load(home, "T-1").phase == Phase.IMPLEMENTING.value

    sessions2 = DryRunSessions()
    report2 = disp.dispatch(cfg, sessions2, now=2600)
    assert report2.spawned == ["T-1"]
    assert sessions2.spawned[0][1].startswith("/maestro-reconcile-implementing T-1")

    # -- implementing (fix round): record the turn, fix, re-attest, hand off again --
    turn = ops.record_impl_turn(cfg, "T-1", role="implementer")
    assert turn["parked"] is False
    rc = cli.main(["--home", str(home), "verify-ac", "T-1", "--ac", "1", "--what", "ran pytest",
                   "--where", "tests/test_widget.py", "--result", "PASSED (export fixed)"])
    assert rc == 0
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "qa", "--requeue", "300"])
    assert rc == 0

    # -- qa round 2: PASS, routes onward to awaiting-ci --
    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1", "--verdict", "pass",
                   "--evidence", "diff now exports widget.py's public function; test green"])
    assert rc == 0
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0
    snap = snap_mod.load(home, "T-1")
    assert snap.phase == Phase.AWAITING_CI.value

    # AC2: two distinct AcQaVerdict events for AC #1, at two different observed_seq.
    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert [e["payload"]["verdict"] for e in qa_events] == ["fail", "pass"]
    assert len({e["seq"] for e in qa_events}) == 2

    spec_text = store.spec_path(home, "T-1").read_text(encoding="utf-8")
    assert snap.qa_failing_acs(spec_text) == []  # the later pass wins


# ---------------------------------------------------------------------------
# AC3: two verdicts for the SAME AC, recorded on two separate visits to `qa`
# (a fail round, a fix, then a pass round -- RF-7's real routing) fold to ONE
# logical verdict for gate purposes -- the phase-keyed step-id
# (record_qa_verdict) keeps them as two distinct events (proven above, AC2),
# but the snapshot fold only ever tracks the latest verdict per ac_hash, so
# the fail-then-pass round trip never double-counts a vote. (T-85: both
# verdicts are recorded from the `qa` phase itself -- the old pre-T-85 shape,
# a verdict recorded while `snap.phase == "implementing"`, is now refused
# outright; see test_qa_phase_gate.py.)
# ---------------------------------------------------------------------------

def test_verdict_across_phase_change_does_not_double_count_for_gate(cfg):
    home = cfg.home
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    ops.record_qa_verdict(cfg, "T-1", 1, "fail", "diff never exports widget.py's public function")
    try:
        ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="tests green")
        assert False, "expected the fail verdict to block awaiting-ci"
    except store.MaestroError as e:
        assert "refusing awaiting-ci" in str(e)

    # The fail sends the ticket back to `implementing` for a fix, then it
    # crosses into `qa` again (RF-7's real routing) and gets re-verdicted there.
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="qa: fail")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off again")
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "diff now exports widget.py's public function")

    events = event_log.read(home, "T-1")
    qa_events = [e for e in events if e["type"] == "AcQaVerdict"]
    assert len(qa_events) == 2  # two distinct events, not a collision
    assert len({e["seq"] for e in qa_events}) == 2

    # The gate reflects only the LATEST verdict -- one logical vote, not two.
    ops.verify_ac(cfg, "T-1", 1, {"what": "ran pytest", "where": "tests/test_widget.py",
                                   "result": "PASSED"})
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="qa: all ACs pass")
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC4: `set-phase awaiting-ci` over the real CLI exits non-zero and appends NO
# event while a current spec-axis verdict is fail; succeeds once a later pass
# supersedes it.
# ---------------------------------------------------------------------------

def test_cli_set_phase_awaiting_ci_refuses_on_fail_then_succeeds_on_pass(home, cfg):
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")
    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off")

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1", "--verdict", "fail",
                   "--evidence", "missing export"])
    assert rc == 0
    before = event_log.read(home, "T-1")

    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc != 0
    after = event_log.read(home, "T-1")
    assert after == before  # no event appended on refusal

    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1", "--verdict", "pass",
                   "--evidence", "export fixed, tests green"])
    assert rc == 0
    cli.main(["--home", str(home), "verify-ac", "T-1", "--ac", "1", "--what", "ran pytest",
              "--where", "tests/test_widget.py", "--result", "PASSED"])
    rc = cli.main(["--home", str(home), "set-phase", "T-1", "awaiting-ci",
                   "--reason", "qa: all ACs pass"])
    assert rc == 0
    assert snap_mod.load(home, "T-1").phase == Phase.AWAITING_CI.value


# ---------------------------------------------------------------------------
# AC7: `health.spawn_rate` counts a real implement+qa round as the real spawn
# count it is now, not the old `1 + max_impl_turns * (...)` preventive estimate.
# ---------------------------------------------------------------------------

def test_spawn_rate_counts_a_real_implement_qa_round_as_real_spawns(home):
    from maestro import health
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0)
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    sessions = _EphemeralSessions()  # gone by the next sweep -- so each sweep re-spawns
    disp.dispatch(cfg, sessions, now=1000)  # spawn #1: implementing, weight 1

    ops.set_phase(cfg, "T-1", Phase.QA, reason="handing off", expect=None)
    disp.dispatch(cfg, sessions, now=2000)  # spawn #2: qa, weight 1 (standards off)

    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="qa: fail")
    disp.dispatch(cfg, sessions, now=3000)  # spawn #3: implementing again, weight 1

    rate = health.spawn_rate(home, 3000)["total"]
    # Exactly 3 real, separately-counted spawns -- not the old estimate, which
    # would have inflated the FIRST `implementing` spawn alone to
    # `1 + max_impl_turns` (21 by default) agent-equivalents.
    assert rate == 3
    assert rate != 1 + cfg.max_impl_turns


# ---------------------------------------------------------------------------
# AC8: a ticket that ping-pongs implementing<->qa past `max_impl_turns` is
# failed by the EXISTING counter (`ops.record_impl_turn`), over real sweeps --
# no new counter is introduced.
# ---------------------------------------------------------------------------

def test_ping_pong_past_max_impl_turns_fails_via_existing_counter_over_real_sweeps(home):
    cfg = Config(home=home, max_concurrency=1, min_spawn_interval=0, max_impl_turns=2)
    _create(cfg, "T-1")
    ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason="worktree ready")

    sessions = _EphemeralSessions()  # gone by the next sweep -- so each sweep re-spawns
    now = 1000
    parked = False
    for round_n in range(1, 5):  # generously exceeds the ceiling of 2
        disp.dispatch(cfg, sessions, now=now)  # a real sweep spawns `implementing`
        now += 600
        turn = ops.record_impl_turn(cfg, "T-1", role="implementer")
        if turn["parked"]:
            parked = True
            break
        ops.set_phase(cfg, "T-1", Phase.QA, reason=f"round {round_n}: handing off")
        disp.dispatch(cfg, sessions, now=now)  # a real sweep spawns `qa`
        now += 600
        ops.record_qa_verdict(cfg, "T-1", 1, "fail", f"still broken, round {round_n}")
        ops.set_phase(cfg, "T-1", Phase.IMPLEMENTING, reason=f"qa: fail round {round_n}")

    assert parked is True
    snap = snap_mod.load(home, "T-1")
    events = event_log.read(home, "T-1")
    failed_events = [e for e in events if e["type"] == "Failed"]
    assert len(failed_events) == 1  # ops.fail's own event -- the existing counter, not a new one
    assert snap.failure_count == 1
    assert snap.impl_turns == cfg.max_impl_turns
