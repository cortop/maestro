"""T-115: `post_qa_skill` -- a config-referenced slash-command skill the
dispatcher fires exactly once per QA pass, at the `qa -> awaiting-ci` trigger
point (every current-hash AC carrying a passing spec-axis QA verdict). Covers
all 5 spec ACs:

  AC1: config.load() accepts the knob at board level and as a
       `[repos.<name>]` override; a malformed value fails closed.
  AC2: a real dispatch() sweep spawns exactly one session for a ticket that
       just routed qa -> awaiting-ci with all ACs passing, and a second sweep
       is a no-op (idempotent event).
  AC3: a qa -> implementing route (a failed AC) never fires it; a LATER
       successful QA pass on a new tree fires it again, once.
  AC4: with the knob unset, the sweep is byte-identical (no event, no spawn).
  AC5 (doc) is covered by tests/test_diagram.py's drift guard on the
       generated docs/dispatch-gates.md, not here.
"""
from __future__ import annotations

import pytest

from maestro import config as config_mod, dispatcher as disp, event_log, ops
from maestro import repos as repos_mod, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

AC_TEXT = "- [ ] the widget works"


def _seed_ticket(home, key):
    store.atomic_write(store.spec_path(home, key), f"# {key}\n\n## Acceptance criteria\n{AC_TEXT}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PrOpened",
                     {"number": 1, "url": "https://github.com/x/y/pull/1", "draft": True}, actor="r")
    snap_mod.rebuild(home, key)


def _qa_pass_to_awaiting_ci(cfg, key, evidence="looks right"):
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
    snap_mod.rebuild(cfg.home, key)
    ops.record_qa_verdict(cfg, key, 1, "pass", evidence)
    event_log.append(cfg.home, key, "PhaseChanged",
                     {"phase": Phase.AWAITING_CI.value, "reason": "qa: all ACs pass"}, actor="r")
    snap_mod.rebuild(cfg.home, key)


def _skill_spawns(sessions, skill="/my-pr-polish"):
    """Spawns invoking *skill* specifically -- as opposed to `sessions.spawned`
    as a whole, which also carries the ordinary reconciler spawn a real
    dispatch() sweep legitimately makes for a ticket sitting in an ACTIVE
    phase (e.g. `implementing`) alongside whatever `sync_post_qa_skill` did."""
    return [s for s in sessions.spawned if s[1].split(" ", 1)[0] == skill]


def _qa_fail_to_implementing(cfg, key, evidence="doesn't work"):
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": Phase.QA.value}, actor="r")
    snap_mod.rebuild(cfg.home, key)
    ops.record_qa_verdict(cfg, key, 1, "fail", evidence)
    event_log.append(cfg.home, key, "PhaseChanged",
                     {"phase": Phase.IMPLEMENTING.value, "reason": "qa: AC failed"}, actor="r")
    snap_mod.rebuild(cfg.home, key)


# ---------------------------------------------------------------------------
# AC1: config.load() accepts the knob board-wide and per-repo; fails closed
# on a malformed value.
# ---------------------------------------------------------------------------

def test_config_accepts_post_qa_skill_board_wide(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\npost_qa_skill = "/my-pr-polish"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.post_qa_skill == "/my-pr-polish"
    binding = repos_mod.implicit_default(cfg)
    assert binding.post_qa_skill == "/my-pr-polish"


def test_config_accepts_post_qa_skill_repo_override(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\npost_qa_skill = "/board-wide"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\npost_qa_skill = "/repo-specific"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["post_qa_skill"] == "/repo-specific"
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.post_qa_skill == "/repo-specific"


def test_config_repo_table_post_qa_skill_unset_inherits_board_wide(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\npost_qa_skill = "/board-wide"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["post_qa_skill"] is None
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.post_qa_skill == "/board-wide"


@pytest.mark.parametrize("bad", ["my-pr-polish", "/", "/1abc", "/bad name", "//x"])
def test_config_rejects_malformed_post_qa_skill_board_wide(home, bad):
    (home / "config.toml").write_text(
        f'[maestro]\nrepo_path = "/repo/default"\npost_qa_skill = "{bad}"\n', encoding="utf-8")
    with pytest.raises(store.MaestroError, match="post_qa_skill"):
        config_mod.load(str(home))


def test_config_rejects_malformed_post_qa_skill_repo_override(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\npost_qa_skill = "no-leading-slash"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError, match="post_qa_skill"):
        config_mod.load(str(home))


def test_config_unset_post_qa_skill_is_none(home):
    (home / "config.toml").write_text('[maestro]\nrepo_path = "/repo/default"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.post_qa_skill is None
    assert repos_mod.implicit_default(cfg).post_qa_skill is None


# ---------------------------------------------------------------------------
# AC2: a real dispatch() sweep spawns exactly once, idempotently.
# ---------------------------------------------------------------------------

def test_sweep_spawns_post_qa_skill_once_after_qa_pass(home, cfg):
    cfg.post_qa_skill = "/my-pr-polish"
    _seed_ticket(home, "T-1")
    _qa_pass_to_awaiting_ci(cfg, "T-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)

    assert len(sessions.spawned) == 1
    key, prompt, *_ = sessions.spawned[0]
    assert key == "T-1"
    assert prompt == "/my-pr-polish T-1"

    spawned_events = [e for e in event_log.read(home, "T-1") if e["type"] == "PostQaSkillSpawned"]
    assert len(spawned_events) == 1

    # A second sweep -- even a fresh DryRunSessions instance, standing in for
    # a crashed-and-respawned dispatcher process with no in-memory state --
    # must not spawn again: the idempotent event is what carries the memory.
    sessions2 = DryRunSessions()
    disp.dispatch(cfg, sessions2, now=2000)
    assert sessions2.spawned == []
    spawned_events_again = [e for e in event_log.read(home, "T-1") if e["type"] == "PostQaSkillSpawned"]
    assert len(spawned_events_again) == 1


def test_sweep_spawns_for_a_ticket_already_sitting_in_in_review(home, cfg):
    """Level-triggered, not edge-triggered (dispatcher module docstring): a
    ticket that had already advanced past `awaiting-ci` to `in-review` by the
    time this sweep runs still fires -- current qa_all_passing state is what
    matters, not which literal phase edge was observed."""
    cfg.post_qa_skill = "/my-pr-polish"
    _seed_ticket(home, "T-1")
    _qa_pass_to_awaiting_ci(cfg, "T-1")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IN_REVIEW.value, "reason": "CI passing"},
                     actor="dispatcher")
    snap_mod.rebuild(home, "T-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)
    assert len(sessions.spawned) == 1


# ---------------------------------------------------------------------------
# AC3: a failed-AC round never fires; a later successful pass on a new tree
# fires again, once.
# ---------------------------------------------------------------------------

def test_qa_fail_round_never_fires_the_skill(home, cfg):
    cfg.post_qa_skill = "/my-pr-polish"
    _seed_ticket(home, "T-1")
    _qa_fail_to_implementing(cfg, "T-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)

    assert _skill_spawns(sessions) == []
    assert not any(e["type"] == "PostQaSkillSpawned" for e in event_log.read(home, "T-1"))


def test_later_successful_qa_pass_on_a_new_tree_fires_again_once(home, cfg):
    cfg.post_qa_skill = "/my-pr-polish"
    _seed_ticket(home, "T-1")

    # Round 1: a clean QA pass -> fires once.
    _qa_pass_to_awaiting_ci(cfg, "T-1", evidence="round 1 evidence")
    sessions1 = DryRunSessions()
    disp.dispatch(cfg, sessions1, now=1000)
    assert len(sessions1.spawned) == 1

    # Round 2: CI/review sends it back, a re-QA round fails an AC -> no fire.
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.IMPLEMENTING.value,
                                                    "reason": "changes requested: fix it"},
                     actor="dispatcher")
    snap_mod.rebuild(home, "T-1")
    _qa_fail_to_implementing(cfg, "T-1", evidence="still broken")
    sessions2 = DryRunSessions()
    disp.dispatch(cfg, sessions2, now=2000)
    assert _skill_spawns(sessions2) == []

    # Round 3: a fresh, genuinely different QA pass (new evidence -> new tree
    # fingerprint) -> fires again, a SECOND time.
    _qa_pass_to_awaiting_ci(cfg, "T-1", evidence="round 3 evidence, now fixed")
    sessions3 = DryRunSessions()
    disp.dispatch(cfg, sessions3, now=3000)
    assert len(sessions3.spawned) == 1

    spawned_events = [e for e in event_log.read(home, "T-1") if e["type"] == "PostQaSkillSpawned"]
    assert len(spawned_events) == 2
    assert spawned_events[0]["payload"]["tree_key"] != spawned_events[1]["payload"]["tree_key"]


# ---------------------------------------------------------------------------
# AC4: unset knob -> byte-identical (no event, no spawn).
# ---------------------------------------------------------------------------

def test_unset_knob_produces_no_event_and_no_spawn(home, cfg):
    assert cfg.post_qa_skill is None
    _seed_ticket(home, "T-1")
    _qa_pass_to_awaiting_ci(cfg, "T-1")
    before = event_log.read(home, "T-1")

    sessions = DryRunSessions()
    disp.dispatch(cfg, sessions, now=1000)

    assert sessions.spawned == []
    after = event_log.read(home, "T-1")
    assert [e["type"] for e in after] == [e["type"] for e in before]
    assert not any(e["type"] == "PostQaSkillSpawned" for e in after)
