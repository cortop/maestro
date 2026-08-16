"""RB-12/RB-14: gate `implementing -> qa` on a captured, current, passing test
run, not on the implementer's word.

Nothing anywhere previously checked that the project's test suite actually
passes -- an implementer could hand off to `qa` with a red suite simply by not
running it (measured twice, 2026-08-14/15: qwen3-coder left a generated doc
stale, Laguna XS 2.1 renamed a referenced symbol -- both advanced to `qa`
anyway, self-reporting success in prose). `[maestro] test_command` (unset by
default -- ships dark) makes maestro itself run the command (a real
subprocess, its own exit code) and gates the transition on a PASSING record
that matches the CURRENT tree state (HEAD sha + a hash of the dirty tree) --
never a free-text self-attestation.

RB-12 (T-71) enforced this by RAISING (refusing `implementing -> qa`, forceable
by a human). RB-14 (T-74) replaced that raise with a transparent REDIRECT to
`verifying` instead -- `ops.set_phase` never refuses this transition anymore;
it silently routes to `verifying`, where the DISPATCHER (`dispatcher.
sync_test_runs`, see tests/test_dispatcher_test_runs.py) runs the suite itself
and routes on to `qa`/`implementing` once it has an answer. `--force` no longer
has any effect on this specific gate (there's nothing left to force past) --
this file's tests were rewritten accordingly; the async dispatcher-owned
runner itself is covered separately.

Every test here drives the real `ops`/`cli` surface against a real throwaway
git repo (`conftest.make_origin_and_repo`) and a real worktree
(`ops.worktree_ensure`) -- never mocked, per this repo's own QA convention.
"""
from __future__ import annotations

import sys

from maestro import cli, config as config_mod, dispatcher as disp, event_log, ops, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo

_PASS_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'
_FAIL_CMD = f'{sys.executable} -c "import sys; sys.exit(1)"'


def _write_config(home, repo, *, test_command=None):
    tc_line = f"test_command = {test_command!r}\n" if test_command is not None else ""
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "{repo}"\nbranch_prefix = "maestro/"\n{tc_line}',
        encoding="utf-8")
    return config_mod.load(str(home))


def _seed_ready_implementing(cfg, key, *, acs=("do the thing",)):
    """TicketCreated -> ready -> a REAL worktree -> implementing, exactly the
    path a real board takes before opening a PR and calling `set-phase qa`."""
    home = cfg.home
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: t\napproval_tier: 1\npriority: 2\n\n## Intent\nx\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")
    ops.worktree_ensure(cfg, key)
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="worktree ready")


# ---------------------------------------------------------------------------
# AC1: `test_command` exists, unset by default, and leaves an existing home's
# transitions byte-identical.
# ---------------------------------------------------------------------------

def test_test_command_defaults_unset(cfg):
    assert cfg.test_command is None


def test_test_command_is_configurable(home):
    (home / "config.toml").write_text('[maestro]\ntest_command = "pytest -q"\n')
    assert config_mod.load(str(home)).test_command == "pytest -q"


def test_unset_test_command_leaves_implementing_to_qa_unaffected(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo)  # test_command left unset
    _seed_ready_implementing(cfg, "G-1")

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")

    assert ev is not None
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value

    # Proven byte-identical over a REAL dispatcher sweep, this repo's own
    # established pattern for exactly this claim (test_drift_policy.py's
    # `disp.dispatch(cfg, DryRunSessions(), now=...)`) -- not just the direct
    # `ops.set_phase` call above. With `test_command` unset, `_tests_stale_reason`
    # short-circuits to None before ever touching `snap.test_runs`/the worktree,
    # so a full sweep over this now-due `qa` ticket must spawn/report it exactly
    # as it would have before RB-12 existed -- the unconfigured gate never
    # intrudes on dispatch()'s own due-checking/spawn decision.
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert "G-1" in report.spawned
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC2 (RB-14): configured + no passing record for the current tree -> a
# transparent REDIRECT to `verifying`, no raise, no event lost.
# ---------------------------------------------------------------------------

def test_set_phase_qa_redirects_to_verifying_without_a_captured_test_run(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_ready_implementing(cfg, "G-1")

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")

    assert ev is not None
    assert ev["payload"]["phase"] == Phase.VERIFYING.value
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value


def test_set_phase_qa_redirects_to_verifying_via_the_real_cli(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_ready_implementing(cfg, "G-1")

    rc = cli.main(["--home", str(home), "set-phase", "G-1", "qa"])

    assert rc == 0
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value


def test_capture_tests_then_set_phase_qa_skips_the_verifying_detour(tmp_path, home):
    """A record already captured for the CURRENT tree (e.g. a reconciler that
    still chooses to call `capture-tests` itself, RB-12's original path) lets
    `set_phase` go straight to `qa` -- no need for the dispatcher to run
    anything, since the answer is already known."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_ready_implementing(cfg, "G-1")

    result = ops.capture_tests(cfg, "G-1")
    assert result["passed"] is True
    assert result["exit_code"] == 0

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")
    assert ev is not None
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC3: the record is a REAL captured run, not an agent's word -- a raw
# self-attestation (Note, or even a forged raw append) never satisfies it, and
# a real failing exit code redirects to `verifying` even while such a claim
# sits in the log.
# ---------------------------------------------------------------------------

def test_self_attested_success_does_not_satisfy_the_gate(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_FAIL_CMD)
    _seed_ready_implementing(cfg, "G-1")

    # The agent lies about it in prose (exactly what qwen3-coder/Laguna did).
    event_log.append(home, "G-1", "Note",
                     {"text": "The task has been completed successfully. All tests pass."},
                     actor="reconciler")
    snap_mod.rebuild(home, "G-1")

    # maestro's own captured run of the SAME command actually fails.
    result = ops.capture_tests(cfg, "G-1")
    assert result["passed"] is False
    assert result["exit_code"] == 1

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")
    assert ev["payload"]["phase"] == Phase.VERIFYING.value  # not QA -- the lie changes nothing


def test_raw_append_cannot_forge_a_passing_test_run_record(tmp_path, home, capsys):
    """`maestro append --type TestRunCaptured` is ops-owned (denylisted) --
    the only way a record is ever produced is `capture_tests`'s own subprocess
    call, so an agent can't hand-roll a passing record either."""
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_FAIL_CMD)
    _seed_ready_implementing(cfg, "G-1")

    rc = cli.main(["--home", str(home), "append", "G-1", "--type", "TestRunCaptured",
                   "--payload", '{"tree_key": "fake:fake", "command": "x", '
                                '"exit_code": 0, "passed": true}'])
    assert rc != 0
    assert "capture-tests" in capsys.readouterr().err
    assert snap_mod.load(home, "G-1").test_runs == {}


# ---------------------------------------------------------------------------
# AC4: bound to the tree, not the ticket -- one more edit desyncs a prior pass.
# ---------------------------------------------------------------------------

def test_a_passing_record_does_not_survive_a_tree_edit(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_ready_implementing(cfg, "G-1")

    ops.capture_tests(cfg, "G-1")
    ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")
    assert snap_mod.load(home, "G-1").phase == Phase.QA.value

    # A fix round bounces back to implementing, then one more edit lands.
    ops.set_phase(cfg, "G-1", Phase.IMPLEMENTING, reason="qa sent it back")
    wt = store.worktree_path(home, "G-1")
    (wt / "one_more_edit.txt").write_text("edit\n", encoding="utf-8")

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="re-request review")
    assert ev["payload"]["phase"] == Phase.VERIFYING.value  # the stale pass no longer satisfies it


# ---------------------------------------------------------------------------
# AC5: a matching record is reused, never re-run -- counted across two calls
# standing in for two real dispatcher sweeps.
# ---------------------------------------------------------------------------

def test_matching_tree_state_is_reused_not_rerun(tmp_path, home, monkeypatch):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_PASS_CMD)
    _seed_ready_implementing(cfg, "G-1")

    real_run = ops.subprocess.run
    shell_calls = []

    def _counting_run(*args, **kwargs):
        if kwargs.get("shell"):
            shell_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(ops.subprocess, "run", _counting_run)

    first = ops.capture_tests(cfg, "G-1")   # sweep 1: no record yet -> real run
    second = ops.capture_tests(cfg, "G-1")  # sweep 2: same tree -> reused

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(shell_calls) == 1, "the configured command must run exactly once for one tree state"


# ---------------------------------------------------------------------------
# AC6 (RB-14): `--force` no longer has any effect on this gate -- there is
# nothing left to force past (the transition redirects, it never refuses).
# ---------------------------------------------------------------------------

def test_force_no_longer_bypasses_the_redirect_to_verifying(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg = _write_config(home, repo, test_command=_FAIL_CMD)
    _seed_ready_implementing(cfg, "G-1")
    ops.capture_tests(cfg, "G-1")  # a real, captured FAIL

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened", actor="valentin", force=True)

    # force=True still lands in `verifying`, never `qa` -- a red tree can no
    # longer be placed in front of QA by a human overriding this gate (the
    # ticket's own AC6).
    assert "forced_by" not in ev["payload"]
    assert ev["payload"]["phase"] == Phase.VERIFYING.value
    assert snap_mod.load(home, "G-1").phase == Phase.VERIFYING.value


# ---------------------------------------------------------------------------
# AC7: a `mode: local` ticket's implementing -> qa is unaffected by the gate.
# ---------------------------------------------------------------------------

def test_mode_local_ticket_transitions_implementing_to_qa_unaffected(tmp_path, home):
    target = tmp_path / "vault"
    target.mkdir()
    (home / "config.toml").write_text(
        f'[maestro]\ntest_command = {_FAIL_CMD!r}\n\n'
        f'[repos.vault]\npath = "{target}"\nmode = "local"\ndefault = true\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    key = "V-1"
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: 0\n\n## Intent\nx\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "source": "test"}, actor="d")
    snap_mod.rebuild(home, key)
    ops.set_phase(cfg, key, Phase.READY, reason="approved")
    ops.set_phase(cfg, key, Phase.IMPLEMENTING, reason="local target ready")

    ev = ops.set_phase(cfg, key, Phase.QA, reason="review")

    assert ev is not None
    assert snap_mod.load(home, key).phase == Phase.QA.value


# ---------------------------------------------------------------------------
# AC8: reproduces the two measured incidents -- a real subprocess failure of
# each shape redirects implementing -> verifying (never straight to qa),
# over two real ops calls standing in for two real dispatcher sweeps.
# ---------------------------------------------------------------------------

def test_reproduces_the_stale_generated_doc_incident(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "generated.md").write_text("STALE\n", encoding="utf-8")
    checker = repo / "check_doc.py"
    checker.write_text(
        "import pathlib, sys\n"
        "doc = pathlib.Path('docs/generated.md')\n"
        "sys.exit(0 if doc.read_text() == 'GENERATED\\n' else 1)\n",
        encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add stale-doc checker", cwd=repo)
    cfg = _write_config(home, repo, test_command=f"{sys.executable} check_doc.py")
    _seed_ready_implementing(cfg, "G-1")

    result = ops.capture_tests(cfg, "G-1")  # real sweep 1
    assert result["passed"] is False

    ev = ops.set_phase(cfg, "G-1", Phase.QA, reason="pr opened")  # real sweep 2
    assert ev["payload"]["phase"] == Phase.VERIFYING.value


def test_reproduces_the_renamed_symbol_incident(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    (repo / "probe.py").write_text(
        "def _default_runner_probe():\n    return True\n", encoding="utf-8")
    checker = repo / "check_symbol.py"
    checker.write_text(
        "import sys\n"
        "try:\n"
        "    from probe import _renamed_probe  # stale reference to the old name\n"
        "except ImportError:\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n",
        encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "add renamed-symbol checker", cwd=repo)
    cfg = _write_config(home, repo, test_command=f"{sys.executable} check_symbol.py")
    _seed_ready_implementing(cfg, "G-2")

    result = ops.capture_tests(cfg, "G-2")  # real sweep 1
    assert result["passed"] is False

    ev = ops.set_phase(cfg, "G-2", Phase.QA, reason="pr opened")  # real sweep 2
    assert ev["payload"]["phase"] == Phase.VERIFYING.value
