"""A restack still queued when a stacked ticket finishes is dropped: the sweep
never runs `gt sync`/`gt restack`/`gt submit` for a done ticket. Real dispatch
sweep, real `gt` detection; only `gt` itself is a stub on PATH."""
import os

from maestro import claims, event_log, ops, repos as repos_mod, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


def _gt_stub(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    gt_log = tmp_path / "gt.log"
    gt = bin_dir / "gt"
    gt.write_text(f'#!/bin/sh\necho "$@" >> "{gt_log}"\nexit 0\n')
    gt.chmod(0o755)
    return bin_dir, gt_log


def _seed_done_stack_with_queued_restack(cfg, key):
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    for i in range(2):
        base = "main" if i == 0 else f"maestro/{key}-1"
        event_log.append(cfg.home, key, "PrOpened", {
            "number": 100 + i, "url": f"https://github.com/x/y/pull/{100 + i}", "draft": False,
            "stack": {"index": i, "total": 2, "branch": f"maestro/{key}-{i + 1}", "base": base},
        }, actor="r")
    event_log.append(cfg.home, key, "RestackQueued", {"stack_index": 0}, actor="dispatcher")
    snap_mod.rebuild(cfg.home, key)
    ops.finalize(cfg, key, actor="dispatcher")
    return snap_mod.load(cfg.home, key)


def test_done_ticket_with_queued_restack_never_runs_gt(cfg, monkeypatch, tmp_path):
    key = "T-9"
    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / ".graphite_repo_config").write_text("{}\n", encoding="utf-8")
    cfg.repo_path = str(repo_dir)
    bin_dir, gt_log = _gt_stub(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    store.worktree_path(cfg.home, key).mkdir(parents=True)  # a leftover worktree

    snap = _seed_done_stack_with_queued_restack(cfg, key)
    assert snap.phase == Phase.DONE.value
    assert snap.pending_restack_index == 0
    assert repos_mod.resolve_stack_tool(repos_mod.resolve(cfg, cfg.home, key)) == "gt"

    disp.dispatch(cfg, DryRunSessions(), now=1000)

    assert claims.read_claim(cfg.home, key) is None
    assert not gt_log.exists()
