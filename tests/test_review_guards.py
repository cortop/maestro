"""T-294: reviews stay out of ticket-only machinery (post-QA skill, drift sync, doctor, typed answers).

Real sweeps (`dispatch` with `DryRunSessions`), the real `maestro doctor` and `maestro ans` verbs over a
temp home and real git repos. Fixtures are synthetic.
"""
from maestro import cli, dispatcher as disp, event_log, inbox, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import git, make_origin_and_repo, run_doctor, seed_phase, seed_review_ticket

KEY = "T-60"


def _detached_worktree(repo, home, key):
    wt = home / "worktrees" / key
    git("worktree", "add", "-q", "--detach", str(wt), "main", cwd=repo)
    return wt


def test_review_in_awaiting_ci_gets_no_post_qa_session(home, cfg):
    cfg.post_qa_skill = "/my-pr-polish"
    seed_review_ticket(home, KEY, brief=False, phase="awaiting-ci")
    assert snap_mod.load(home, KEY).phase == Phase.AWAITING_CI.value
    sessions = DryRunSessions()
    result = disp.sync_post_qa_skill(cfg, sessions, 1000)
    disp.dispatch(cfg, sessions, now=1000)
    assert result == {"fired": []}
    assert [s for s in sessions.spawned if s[1].startswith("/my-pr-polish")] == []
    assert not [e for e in event_log.read(home, KEY) if e["type"] == "PostQaSkillSpawned"]


def test_review_is_absent_from_drift_skipped(home, cfg, tmp_path):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    seed_review_ticket(home, KEY, brief=False, phase="awaiting-ci")
    _detached_worktree(repo, home, KEY)
    (repo / "NEWS.md").write_text("main moves\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base moves", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    report = disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert KEY not in report.drift_skipped
    assert KEY not in disp.sync_worktrees(cfg)["skipped_by_policy"]


def test_doctor_exempts_review_but_not_ticket(home, cfg, tmp_path):
    _origin, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    seed_review_ticket(home, KEY, brief=False, phase="awaiting-ci")
    _detached_worktree(repo, home, KEY)
    seed_phase(home, "T-7", Phase.AWAITING_CI)
    store.atomic_write(store.spec_path(home, "T-7"), "# T-7\n\n## Intent\nno acs\n")
    _detached_worktree(repo, home, "T-7")
    _code, report = run_doctor(home)
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["missing_acs"]["keys"] == ["T-7"]
    assert [d["key"] for d in checks["worktree_branch"]["detached"]] == ["T-7"]


def test_typed_answer_to_brief_is_consumed_and_question_stays_open(home, cfg):
    seed_review_ticket(home, KEY)
    qid = next(iter(snap_mod.load(home, KEY).open_questions))
    assert cli.main(["--home", str(home), "ans", KEY, "looks fine", "--no-nudge"]) == 0
    assert inbox.has_pending(home, KEY)
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert sessions.spawned == [] and KEY not in report.spawned
    snap = snap_mod.load(home, KEY)
    assert snap.phase == Phase.AWAITING_HUMAN.value and qid in snap.open_questions
    assert not inbox.has_pending(home, KEY)
    notes = [e for e in event_log.read(home, KEY) if e["type"] == "Note"]
    assert any("review actions" in e["payload"]["text"] for e in notes)
    disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert snap_mod.load(home, KEY).phase == Phase.AWAITING_HUMAN.value


def test_stranded_review_is_re_asked_once(home, cfg):
    seed_review_ticket(home, KEY, phase="ready")
    event_log.append(home, KEY, "QuestionAnswered",
                     {"qid": f"review-brief-{KEY}-aaaaaaaaaaaa", "answer": "typed"}, actor="human")
    snap = snap_mod.rebuild(home, KEY)
    assert snap.phase == Phase.READY.value and not snap.open_questions
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    snap = snap_mod.load(home, KEY)
    assert snap.phase == Phase.AWAITING_HUMAN.value
    assert [q for q in snap.open_questions if q.startswith("review-brief-")]
    seq = snap.observed_seq
    disp.dispatch(cfg, DryRunSessions(), now=2000)
    assert snap_mod.load(home, KEY).observed_seq == seq
