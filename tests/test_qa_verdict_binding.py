"""T-209: QA verdicts carry the head/tree they judged; CiObserved carries its head."""
from conftest import add_worktree, make_origin_and_repo

from maestro import dispatcher as disp
from maestro import implementing_brief
from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.statemachine import Phase



def _ticket(cfg, key="T-1"):
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key, "source": "test"}, actor="d")
    snap_mod.rebuild(cfg.home, key)
    ops.set_phase(cfg, key, Phase.QA, reason="handoff")


def _verdicts(cfg, key):
    return [e["payload"] for e in event_log.read(cfg.home, key) if e["type"] == "AcQaVerdict"]


def test_verdict_stamps_head_and_tree(cfg, tmp_path):
    _, repo = make_origin_and_repo(tmp_path)
    cfg.repo_path = str(repo)
    wt = add_worktree(repo, cfg.home, "T-1", "maestro/T-1")
    _ticket(cfg)
    ops.record_qa_verdict(cfg, "T-1", 1, "pass", "ok")
    p = _verdicts(cfg, "T-1")[0]
    import subprocess
    want = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD", "HEAD^{tree}"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert (p["head_sha"], p["tree_sha"]) == tuple(want)
    h = p["ac_hash"]
    assert snap_mod.load(cfg.home, "T-1").qa_verdicts[h]["tree_sha"] == want[1]


def test_legacy_verdict_unbound(cfg):
    _ticket(cfg)
    h = snap_mod.ac_hash("ok")
    event_log.append(cfg.home, "T-1", "AcQaVerdict",
                     {"ac_hash": h, "ac_index": 1, "ac_text": "ok", "verdict": "pass",
                      "evidence": "e", "axis": "spec"}, actor="q")
    snap = snap_mod.rebuild(cfg.home, "T-1")
    assert "head_sha" not in snap.qa_verdicts[h]
    ops.verify_ac(cfg, "T-1", 1, {"what": "w", "where": "t", "result": "r"})
    ops.set_phase(cfg, "T-1", Phase.AWAITING_CI, reason="qa passed")


def test_ci_observed_has_head(cfg):
    _ticket(cfg)
    disp._observe_ci(cfg, "T-1", {"ci_state": "pending", "failing_checks": [], "head_sha": "abc123"},
                     Phase.AWAITING_CI, pr_number=1)
    ev = [e for e in event_log.read(cfg.home, "T-1") if e["type"] == "CiObserved"][-1]
    assert ev["payload"]["head_sha"] == "abc123"


def test_skill_draft_ci_round_goes_to_qa():
    text = " ".join(implementing_brief.instructions("ci-fix").split())
    assert 'maestro handoff "$KEY"' in text
    assert "sends a still-draft PR to `qa`" in text
    # the routing itself is pinned in tests/test_handoff_verb.py


def test_test_run_step_id_keys_command(cfg):
    _ticket(cfg)
    for cmd in ("pytest -q", "pytest -x"):
        ops._record_test_run(cfg, "T-1", tree_key="t:1", command=cmd, exit_code=0, output="",
                             actor="r")
    runs = [e for e in event_log.read(cfg.home, "T-1") if e["type"] == "TestRunCaptured"]
    assert len(runs) == 2
