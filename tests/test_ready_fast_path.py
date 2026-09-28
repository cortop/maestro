"""T-138: the dispatcher's own sweep applying the `ready` phase's fully
scripted rules (dependsOn check, kind/mode branch, `worktree_ensure`,
`set-phase`) directly, instead of spawning a `claude -p` reconciler whose job
never varies. Drives a real `dispatcher.dispatch()` sweep over a temp home
with a REAL git origin+repo (never mocked) for every scenario that touches a
worktree; mocks nothing but the spawn itself (`DryRunSessions`).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import config as config_mod, diagram
from maestro import dispatcher as disp
from maestro import event_log, inbox, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git as _git, make_origin_and_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _create(home, key, *, phase=Phase.READY.value, deps=None, kind=None):
    deps_line = f"dependsOn: [{', '.join(deps)}]\n" if deps else ""
    spec = f"# {key}\n{deps_line}\n## Acceptance criteria\n- [ ] ok\n"
    store.atomic_write(store.spec_path(home, key), spec)
    payload = {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}
    if kind:
        payload["kind"] = kind
    event_log.append(home, key, "TicketCreated", payload, actor="d")
    if phase is not None:
        event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="d")
    snap_mod.rebuild(home, key)


def _dispatcher_phase_changes(home, key):
    return [e for e in event_log.read(home, key)
            if e["type"] == "PhaseChanged" and e["actor"] == "dispatcher"]


def _ledger_decisions(home):
    lines = disp.dispatch_ledger_path(home).read_text().splitlines()
    return json.loads(lines[-1])["decisions"]


def _spawn_ledger(home):
    return store.read_json(disp._spawn_ledger_path(home), {}) or {}


# ---------------------------------------------------------------------------
# AC1: worktree-ready hand-off -- creates the worktree, sets phase, and
# spawns `implementing` in the SAME sweep (T-136's hand-off exemption).
# ---------------------------------------------------------------------------

def test_worktree_ready_hand_off_spawns_implementing_same_sweep(home, cfg, tmp_path):
    cfg.ready_fast_path = "on"
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    key = "T-1"
    _create(home, key, phase=Phase.TRIAGING.value)

    # Last spawned 60s earlier in another phase (triaging) -- a real sweep,
    # not a hand-authored ledger entry.
    first = disp.dispatch(cfg, DryRunSessions(), now=940)
    assert first.spawned == [key]
    assert _spawn_ledger(home)[key]["phase"] == "triaging"

    rc = cli_main(["--home", str(home), "set-phase", key, "ready"])
    assert rc == 0

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.IMPLEMENTING.value
    wt = store.worktree_path(home, key)
    assert wt.is_dir()

    changes = _dispatcher_phase_changes(home, key)
    assert len(changes) == 1
    assert changes[0]["payload"]["phase"] == "implementing"
    assert changes[0]["payload"]["reason"] == "worktree ready"

    # No `ready` reconciler ran -- the only spawn this sweep is `implementing`.
    assert report.spawned == [key]
    match = next(s for s in sessions.spawned if s[0] == key)
    assert match[1].startswith("/maestro-reconcile-implementing")

    # That implementing spawn is the only spawn-ledger write this sweep --
    # the ledger's `recent` list grew by exactly one entry since the seed
    # sweep above (triaging), never two (no separate `ready` spawn slipped
    # in between).
    ledger_entry = _spawn_ledger(home)[key]
    assert len(ledger_entry["recent"]) == 2
    assert ledger_entry["phase"] == "implementing"

    assert _ledger_decisions(home)[key]["outcome"] == "route_implementing"
    assert "`route_implementing`" in diagram.render_dispatch_gates()

    # Idempotent: a second sweep appends no further dispatcher PhaseChanged.
    disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert _dispatcher_phase_changes(home, key) == changes


# ---------------------------------------------------------------------------
# AC2: research / local-mode / blocked-dep / discard -- each produces a
# dispatcher-authored event and zero spawns this sweep (no chaining beyond
# the git-mode "worktree ready" branch above).
# ---------------------------------------------------------------------------

def test_research_kind_routes_to_researching_no_worktree(home, cfg):
    cfg.ready_fast_path = "on"
    key = "T-1"
    _create(home, key, kind="research")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.RESEARCHING.value
    assert not store.worktree_path(home, key).exists()
    changes = _dispatcher_phase_changes(home, key)
    assert len(changes) == 1
    assert changes[0]["payload"]["reason"] == "research ticket: beginning exploration"
    assert key not in report.spawned
    assert _ledger_decisions(home)[key]["outcome"] == "route_researching"


def test_local_mode_routes_to_implementing_no_branch(home, cfg, tmp_path):
    cfg.ready_fast_path = "on"
    local_dir = tmp_path / "notes-vault"
    local_dir.mkdir()
    cfg.repos = {"notes": {"default": True, "mode": "local", "path": str(local_dir)}}
    key = "T-1"
    _create(home, key)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.IMPLEMENTING.value
    changes = _dispatcher_phase_changes(home, key)
    assert len(changes) == 1
    assert changes[0]["payload"]["reason"] == "local target ready"
    assert key not in report.spawned
    assert _ledger_decisions(home)[key]["outcome"] == "route_implementing"


def test_unmet_dependson_consumes_spec_wake_leaves_blocked_dep(home, cfg):
    cfg.ready_fast_path = "on"
    key, dep = "T-1", "T-dep"
    _create(home, dep, phase=Phase.TRIAGING.value)   # not done
    _create(home, key, deps=[dep])

    # A spec edit -- current on-disk hash now differs from the folded one.
    spec_path = store.spec_path(home, key)
    spec_path.write_text(spec_path.read_text() + "\n<!-- edited -->\n", encoding="utf-8")

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert not _dispatcher_phase_changes(home, key)
    assert key not in report.spawned
    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.READY.value
    assert snap.spec_hash == disp.spec_hash_on_disk(home, key)   # SpecObserved landed
    assert _ledger_decisions(home)[key]["outcome"] == "route_blocked_dep"

    # Next sweep: the wake is consumed, so is_due's own blocked-dep short
    # circuit fires directly -- no dispatcher action, `not_due`/"blocked-dep".
    disp.dispatch(cfg, DryRunSessions(), now=1001)
    assert _ledger_decisions(home)[key] == {"outcome": "not_due", "reason": "blocked-dep"}


def test_lone_pending_discard_routes_to_terminating_and_acks(home, cfg):
    cfg.ready_fast_path = "on"
    key = "T-1"
    _create(home, key)
    inbox.append_command(home, key, "discard", {})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.TERMINATING.value
    changes = _dispatcher_phase_changes(home, key)
    assert len(changes) == 1
    assert changes[0]["payload"]["reason"] == "human: discard"
    assert not inbox.pending(home, key)
    assert key not in report.spawned
    assert _ledger_decisions(home)[key]["outcome"] == "route_terminating"


# ---------------------------------------------------------------------------
# AC3: whatever the rules can't decide inline falls back to today's spawn --
# no dispatcher PhaseChanged, no worktree created by the sweep.
# ---------------------------------------------------------------------------

def test_priming_binding_falls_back_to_todays_spawn(home, cfg):
    cfg.ready_fast_path = "on"
    cfg.prime = "true"
    key = "T-1"
    _create(home, key)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == [key]
    assert not _dispatcher_phase_changes(home, key)
    assert not store.worktree_path(home, key).exists()


def test_free_text_command_falls_back_to_todays_spawn(home, cfg):
    cfg.ready_fast_path = "on"
    key = "T-1"
    _create(home, key)
    inbox.append_command(home, key, "msg", {"text": "hold on, checking something"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == [key]
    assert not _dispatcher_phase_changes(home, key)
    assert inbox.pending(home, key)   # not folded by the route -- the spawned skill folds it


def test_pending_ans_with_no_open_question_falls_back_to_todays_spawn(home, cfg):
    cfg.ready_fast_path = "on"
    key = "T-1"
    _create(home, key)
    inbox.append_command(home, key, "ans", {"text": "ok"})

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == [key]
    assert not _dispatcher_phase_changes(home, key)


def test_non_t81_ensure_failure_falls_back_and_records_hook_error(home, cfg):
    cfg.ready_fast_path = "on"
    # No repo_path configured at all -- `worktree_ensure` raises a plain
    # MaestroError ("no path configured"), never the T-81 subclass.
    key = "T-1"
    _create(home, key)

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert report.spawned == [key]
    assert not _dispatcher_phase_changes(home, key)
    assert not store.worktree_path(home, key).exists()
    assert f"worktree_ensure:{key}" in report.hook_errors


# ---------------------------------------------------------------------------
# AC4: a witnessed worktree that fails its own health check parks
# awaiting-human (T-81) instead of falling back -- worktree and files
# untouched, nothing spawns for that key. A second ready ticket in the same
# sweep still routes normally.
# ---------------------------------------------------------------------------

def test_witnessed_unhealthy_worktree_parks_awaiting_human(home, cfg, tmp_path):
    cfg.ready_fast_path = "on"
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    key, key2 = "T-1", "T-2"
    _create(home, key)
    _create(home, key2)

    # A real, completed, witnessed worktree for T-1 -- then trip the
    # mass-deletion health heuristic without touching the witness, exactly
    # the T-81 "creation genuinely finished but now looks wrong" shape.
    ops.worktree_ensure(cfg, key)
    wt = store.worktree_path(home, key)
    assert ops.worktree_health(wt)["healthy"] is True
    (wt / "scratch-notes.md").write_text("work in progress\n", encoding="utf-8")
    for i in range(60):
        (wt / f"seed{i}.txt").write_text(f"{i}\n", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-q", "-m", "seed many files", cwd=wt)
    for i in range(55):
        (wt / f"seed{i}.txt").unlink()
    assert ops.worktree_health(wt)["healthy"] is False

    report = disp.dispatch(cfg, DryRunSessions(), now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.AWAITING_HUMAN.value
    events = event_log.read(home, key)
    asked = [e for e in events if e["type"] == "QuestionAsked"]
    assert len(asked) == 1
    assert asked[0]["payload"]["qid"] == f"wt-{key}"
    assert asked[0]["payload"]["text"].startswith("worktree ensure refused:")
    assert key not in report.spawned
    assert (wt / "scratch-notes.md").read_text(encoding="utf-8") == "work in progress\n"
    assert _ledger_decisions(home)[key]["outcome"] == "worktree_refused"

    # T-1's refusal doesn't block T-2 in the same sweep.
    snap2 = snap_mod.load(home, key2)
    assert snap2.phase == Phase.IMPLEMENTING.value
    assert key2 in report.spawned


# ---------------------------------------------------------------------------
# AC5: the skill calls `inbox-ack`, and the verb grant includes it.
# ---------------------------------------------------------------------------

def test_ready_skill_calls_inbox_ack_and_grant_includes_it():
    assert "inbox-ack" in disp._PHASE_VERB_GRANT_BY_SUFFIX["ready"]
    text = (REPO_ROOT / ".claude" / "commands" / "maestro-reconcile-ready.md").read_text(
        encoding="utf-8")
    assert 'maestro inbox-ack "$KEY"' in text


# ---------------------------------------------------------------------------
# AC6: combined with answer_fast_path=on, dry_run, off-is-byte-identical, and
# the config knob failing closed.
# ---------------------------------------------------------------------------

def test_combined_with_answer_fast_path_collapses_three_phases_into_one_sweep(home, cfg, tmp_path):
    cfg.answer_fast_path = "on"
    cfg.ready_fast_path = "on"
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    key = "T-1"
    _create(home, key, phase=Phase.AWAITING_HUMAN.value)
    ops.ask(cfg, key, "Proceed?")
    inbox.append_command(home, key, "ans", {"text": "ok"})

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.IMPLEMENTING.value
    assert report.spawned == [key]
    assert len(sessions.spawned) == 1
    assert sessions.spawned[0][1].startswith("/maestro-reconcile-implementing")


def test_ready_fast_path_off_reproduces_todays_spawn(home, cfg, tmp_path):
    cfg.answer_fast_path = "on"
    cfg.ready_fast_path = "off"
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    key = "T-1"
    _create(home, key, phase=Phase.AWAITING_HUMAN.value)
    ops.ask(cfg, key, "Proceed?")
    inbox.append_command(home, key, "ans", {"text": "ok"})

    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)

    snap = snap_mod.load(home, key)
    assert snap.phase == Phase.READY.value
    assert report.spawned == [key]
    match = next(s for s in sessions.spawned if s[0] == key)
    assert match[1].startswith("/maestro-reconcile-ready")
    assert _ledger_decisions(home)[key]["outcome"] == "answer_routed"


def test_dry_run_previews_without_side_effects(home, cfg, tmp_path):
    cfg.ready_fast_path = "on"
    origin, repo = make_origin_and_repo(tmp_path, name="target")
    cfg.repo_path = str(repo)
    key = "T-1"
    _create(home, key)

    disp.dispatch(cfg, DryRunSessions(), now=1000, dry_run=True)

    assert not store.worktree_path(home, key).exists()
    assert not _dispatcher_phase_changes(home, key)
    assert snap_mod.load(home, key).phase == Phase.READY.value
    assert _ledger_decisions(home)[key]["outcome"] == "would_route_implementing"


def test_unknown_ready_fast_path_value_fails_closed(home):
    (home / "config.toml").write_text('[maestro]\nready_fast_path = "shadow"\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "ready_fast_path" in msg and "shadow" in msg
    for valid in ("off", "on"):
        assert valid in msg
