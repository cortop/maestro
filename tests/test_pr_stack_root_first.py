"""T-131: a split PR stack (T-126) must be opened, and undrafted for review,
strictly root-first -- otherwise reviewers can see a later entry ready while
its own base is still a draft or hasn't been opened at all (seen live on
T-126, `undraft-T-126-252`). Two independent guards enforce this:

- `cli.cmd_append` refuses a `PrOpened` at `stack.index = n > 0` unless entry
  `n-1` is already recorded (AC1).
- `dispatcher.sync_vcs`'s undraft path never calls `gh pr ready` on entry `n`
  while an earlier entry is still a draft and unmerged (AC2), so entries
  become ready in index order across sweeps (AC3) -- and a stack observed
  out of that order anyway (e.g. a human ran `gh pr ready` directly) gets one
  deduped `Note` naming both PR numbers instead of being left as-is (AC4).
"""
import json

from maestro import cli, event_log, ops, providers, snapshot as snap_mod, store
from maestro import dispatcher as disp
from maestro.statemachine import Phase

from conftest import seed_ticket


# ---------------------------------------------------------------------------
# AC1: the append-time root-first guard.
# ---------------------------------------------------------------------------

def test_append_pr_opened_stack_index_gt_zero_refused_without_prior_entry(home, cfg, capsys):
    seed_ticket(home, "T-1", "stacked PR ticket", phase="implementing")
    payload = json.dumps({
        "number": 101, "url": "https://github.com/x/y/pull/101", "draft": True,
        "stack": {"index": 1, "total": 2, "branch": "maestro/T-1-2", "base": "maestro/T-1-1"},
    })

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", "PrOpened",
                   "--payload", payload, "--step-id", "pr-stack-T-1-0"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "stack.index=1" in err
    assert "root-first" in err
    assert [e for e in event_log.read(home, "T-1") if e["type"] == "PrOpened"] == []


def test_append_pr_opened_stack_index_gt_zero_succeeds_once_prior_entry_present(home, cfg):
    seed_ticket(home, "T-1", "stacked PR ticket", phase="implementing")
    entry0 = json.dumps({
        "number": 100, "url": "https://github.com/x/y/pull/100", "draft": True,
        "stack": {"index": 0, "total": 2, "branch": "maestro/T-1-1", "base": "main"},
    })
    rc0 = cli.main(["--home", str(home), "append", "T-1", "--type", "PrOpened",
                    "--payload", entry0, "--step-id", "pr-T-1"])
    assert rc0 == 0

    entry1 = json.dumps({
        "number": 101, "url": "https://github.com/x/y/pull/101", "draft": True,
        "stack": {"index": 1, "total": 2, "branch": "maestro/T-1-2", "base": "maestro/T-1-1"},
    })
    rc1 = cli.main(["--home", str(home), "append", "T-1", "--type", "PrOpened",
                    "--payload", entry1, "--step-id", "pr-stack-T-1-0"])

    assert rc1 == 0
    snap = snap_mod.load(home, "T-1")
    assert {e["index"] for e in snap.pr_stack} == {0, 1}


def test_append_plain_pr_opened_with_no_stack_metadata_is_unaffected(home, cfg):
    """A non-split (plain) PrOpened carries no `stack` key at all -- the
    root-first guard must never fire for it, byte-identical to before this
    ticket."""
    seed_ticket(home, "T-1", "ordinary ticket", phase="implementing")
    payload = json.dumps({"number": 7, "url": "https://github.com/x/y/pull/7", "draft": True})

    rc = cli.main(["--home", str(home), "append", "T-1", "--type", "PrOpened",
                   "--payload", payload, "--step-id", "pr-T-1"])

    assert rc == 0
    assert len([e for e in event_log.read(home, "T-1") if e["type"] == "PrOpened"]) == 1


# ---------------------------------------------------------------------------
# AC2/AC3/AC4: the dispatcher-side root-first undraft guard + drift detection.
# ---------------------------------------------------------------------------

class FakeVCS:
    """The only mock: the external GitHub boundary. `pr_ready` records every
    call and, on success, flips the entry's own `statuses[pr]["draft"]` to
    False -- standing in for GitHub's real state actually changing, so a
    later poll in the same test sees the undrafted PR exactly like the real
    API would."""

    def __init__(self, statuses, reviews=None):
        self.statuses = {k: dict(v) for k, v in statuses.items()}
        self.reviews = reviews or {}
        self.ready_calls: list[int] = []

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return dict(self.statuses[pr_number])

    def review_feedback(self, pr_number, repo=None, env=None):
        return self.reviews.get(pr_number, [])

    def pr_ready(self, pr_number, repo=None, env=None):
        self.ready_calls.append(pr_number)
        self.statuses[pr_number]["draft"] = False
        return {"ok": True}


def _use_fake(cfg, monkeypatch, fake, *, interval=0):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": interval}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _seed_stack(cfg, key, phase, entries, *, qa_pass=False):
    """*entries*: list of (pr_number, draft) in index order. `qa_pass=True`
    records a passing spec-axis QA verdict (via a real `qa` phase detour,
    same idiom as `test_undraft.py`'s `_seed`) so the tracked entry (index 0)
    is actually undraft-eligible through `_maybe_undraft`'s own gate."""
    store.atomic_write(store.spec_path(cfg.home, key),
                       f"# {key}\n\n## Acceptance criteria\n- [ ] ok\n")
    spec_hash = disp.spec_hash_on_disk(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated", {"title": key, "spec_hash": spec_hash},
                     actor="d")
    n = len(entries)
    for i, (pr, draft) in enumerate(entries):
        base = "main" if i == 0 else f"maestro/{key}-{i}"
        event_log.append(cfg.home, key, "PrOpened", {
            "number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": draft,
            "stack": {"index": i, "total": n, "branch": f"maestro/{key}-{i+1}", "base": base},
        }, actor="r")
    if qa_pass:
        event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
        snap_mod.rebuild(cfg.home, key)
        ops.record_qa_verdict(cfg, key, 1, "pass", "looks right")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def test_sync_vcs_never_undrafts_tip_while_root_still_draft(cfg, monkeypatch):
    """AC2: the tip (index 1) is itself CI-passing -- otherwise
    undraft-eligible -- but the root (index 0) is still a draft and unmerged,
    so `gh pr ready` must never be called for the tip."""
    fake = FakeVCS({
        100: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "passing", "failing_checks": [], "draft": True},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "passing", "failing_checks": [], "draft": True},
    })
    _use_fake(cfg, monkeypatch, fake)
    # qa_pass=False -- root also never undrafts (missing QA verdict), but
    # that's incidental here; the point is the TIP specifically never does.
    _seed_stack(cfg, "T-5", Phase.AWAITING_CI, [(100, True), (101, True)])

    disp.sync_vcs(cfg, now=1000)

    assert 101 not in fake.ready_calls
    snap = snap_mod.load(cfg.home, "T-5")
    tip = next(e for e in snap.pr_stack if e["index"] == 1)
    assert tip["draft"] is True


def test_sync_vcs_undrafts_root_first_then_tip_on_a_later_sweep(cfg, monkeypatch):
    """AC3: once the root becomes eligible it undrafts first; the tip -- even
    though it's independently eligible too -- waits for a LATER sweep, since
    this sweep's own root-first check reads the stack as it stood before this
    tick's own root undraft."""
    fake = FakeVCS({
        100: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "passing", "failing_checks": [], "draft": True},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "passing", "failing_checks": [], "draft": True},
    })
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed_stack(cfg, "T-5", Phase.AWAITING_CI, [(100, True), (101, True)], qa_pass=True)

    disp.sync_vcs(cfg, now=1000)

    assert fake.ready_calls == [100]
    snap = snap_mod.load(cfg.home, "T-5")
    root = next(e for e in snap.pr_stack if e["index"] == 0)
    tip = next(e for e in snap.pr_stack if e["index"] == 1)
    assert root["draft"] is False
    assert tip["draft"] is True  # not yet -- this sweep's stack still saw root as draft

    disp.sync_vcs(cfg, now=2000)

    assert fake.ready_calls == [100, 101]
    snap = snap_mod.load(cfg.home, "T-5")
    tip = next(e for e in snap.pr_stack if e["index"] == 1)
    assert tip["draft"] is False


def test_sync_vcs_detects_out_of_order_stack_and_notes_once(cfg, monkeypatch):
    """AC4: the tip is ALREADY observed ready (draft False) while the root is
    still a draft and unmerged -- this can only happen from outside maestro's
    own guarded undraft paths (e.g. a human ran `gh pr ready` on it
    directly). One deduped Note names both PR numbers; a repeat sweep with
    the same drift doesn't note it again."""
    fake = FakeVCS({
        100: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "passing", "failing_checks": [], "draft": False},
    })
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed_stack(cfg, "T-5", Phase.AWAITING_CI, [(100, True), (101, True)])

    disp.sync_vcs(cfg, now=1000)

    notes = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "Note"]
    order_notes = [n for n in notes if "out of order" in n["payload"].get("text", "")]
    assert len(order_notes) == 1
    assert "100" in order_notes[0]["payload"]["text"]
    assert "101" in order_notes[0]["payload"]["text"]

    disp.sync_vcs(cfg, now=2000)  # same drift observed again -- deduped, not re-noted

    notes_again = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "Note"]
    order_notes_again = [n for n in notes_again if "out of order" in n["payload"].get("text", "")]
    assert len(order_notes_again) == 1


def test_sync_vcs_stack_never_reports_out_of_order_when_actually_in_order(cfg, monkeypatch):
    """A ready root with a still-draft tip is the NORMAL, in-order state --
    must never spuriously Note."""
    fake = FakeVCS({
        100: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha100",
              "ci_state": "passing", "failing_checks": [], "draft": False},
        101: {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha101",
              "ci_state": "unknown", "failing_checks": [], "draft": True},
    })
    _use_fake(cfg, monkeypatch, fake, interval=0)
    _seed_stack(cfg, "T-5", Phase.AWAITING_CI, [(100, False), (101, True)], qa_pass=True)

    disp.sync_vcs(cfg, now=1000)

    notes = [e for e in event_log.read(cfg.home, "T-5") if e["type"] == "Note"]
    assert [n for n in notes if "out of order" in n["payload"].get("text", "")] == []
