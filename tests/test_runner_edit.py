"""UX-1: `gates.runner_editable` (the one shared guard), `ops.set_runner` (the
one write-through), and the `maestro runner <KEY>` CLI verb that calls it --
changing a ticket's `runner:`/`runner_model:` spec front-matter before
implementation begins.
"""
from __future__ import annotations

import difflib

import pytest

from maestro import claims, dispatcher as disp, event_log, gates, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.statemachine import Phase

from test_claims import child_process  # noqa: F401 -- shared real-process fixture

SPEC = (
    "# T-1: Sample\n"
    "\n"
    "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->\n"
    "\n"
    "approval_tier: 1\n"
    "priority: 3\n"
    "dependsOn: []\n"
    "\n"
    "## Intent\n"
    "Do the thing.\n"
    "\n"
    "## Acceptance criteria\n"
    "- [ ] it works\n"
)


def _seed_ticket(home, key, *, phase=Phase.READY, spec=SPEC):
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


# --- gates.runner_editable -----------------------------------------------------

@pytest.mark.parametrize("phase", [Phase.TRIAGING, Phase.AWAITING_HUMAN, Phase.READY,
                                   Phase.IMPLEMENTING, Phase.QA, Phase.AWAITING_CI,
                                   Phase.IN_REVIEW, Phase.RESEARCHING])
def test_editable_true_with_no_live_claim_regardless_of_phase(home, phase):
    """T-54: phase-aware routing means no fixed phase whitelist is still
    correct -- a runner can now be admitted to `qa`/`researching`/`triaging`
    too, and a ticket can cycle back through `implementing` more than once
    (a QA fix round), so the only thing that still freezes an edit is a
    session actually in flight RIGHT NOW (see the next two tests)."""
    _seed_ticket(home, "T-1", phase=phase)
    snap = snap_mod.load(home, "T-1")
    assert gates.runner_editable(home, "T-1", snap) is True


def test_editable_true_even_with_worktree_on_disk_if_no_live_claim(home):
    """T-54: worktree existence alone no longer blocks an edit -- a worktree
    persists for the ticket's whole lifetime once created (including across
    QA fix rounds), so it isn't evidence a runner-bearing spawn is in flight;
    only a CONFIRMED claim is."""
    _seed_ticket(home, "T-1", phase=Phase.READY)
    store.worktree_path(home, "T-1").mkdir(parents=True)
    snap = snap_mod.load(home, "T-1")
    assert gates.runner_editable(home, "T-1", snap) is True


def test_editable_false_with_confirmed_claim_and_claim_file_survives(home, child_process):
    """AC7: the predicate must not release a stale/confirmed claim as a side
    effect (unlike `claims.is_claimed`) -- the claim file is still on disk
    after the call."""
    _seed_ticket(home, "T-1", phase=Phase.READY)
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    snap = snap_mod.load(home, "T-1")

    assert gates.runner_editable(home, "T-1", snap) is False
    assert claims.claim_path(home, "T-1").exists()


def test_editable_false_with_confirmed_claim_even_mid_implementing_fix_round(home, child_process):
    """AC7: "refuses one for a phase currently mid-spawn" -- a confirmed-live
    claim refuses the edit regardless of which phase the ticket is in,
    including a SECOND pass through `implementing` (a QA fix round)."""
    _seed_ticket(home, "T-1", phase=Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    snap = snap_mod.load(home, "T-1")

    assert gates.runner_editable(home, "T-1", snap) is False


# --- ops.set_runner -------------------------------------------------------------

def test_set_runner_inserts_new_lines_surgically(home, cfg):
    _seed_ticket(home, "T-1")
    spec_path = store.spec_path(home, "T-1")
    before = spec_path.read_text()

    result = ops.set_runner(cfg, "T-1", runner="opencode", runner_model="qwen3-coder:30b")

    after = spec_path.read_text()
    assert result["runner"] == "opencode"
    assert result["runner_model"] == "qwen3-coder:30b"
    assert "runner: opencode" in after
    assert "runner_model: qwen3-coder:30b" in after

    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
    added = [l for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
    assert added == ["+runner: opencode", "+runner_model: qwen3-coder:30b"]
    assert removed == []


def test_set_runner_replaces_existing_line_in_place(home, cfg):
    spec = SPEC.replace("priority: 3\n", "priority: 3\nrunner: claude\n")
    _seed_ticket(home, "T-1", spec=spec)
    spec_path = store.spec_path(home, "T-1")
    before = spec_path.read_text()

    ops.set_runner(cfg, "T-1", runner="opencode")

    after = spec_path.read_text()
    assert after.count("runner:") == 1
    assert "runner: opencode" in after
    assert "runner_model:" not in after
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
    changed = [l for l in diff if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    assert changed == ["-runner: claude", "+runner: opencode"]


def test_set_runner_only_one_field_given_leaves_the_other_untouched(home, cfg):
    spec = SPEC.replace("priority: 3\n", "priority: 3\nrunner: opencode\nrunner_model: old:1b\n")
    _seed_ticket(home, "T-1", spec=spec)

    ops.set_runner(cfg, "T-1", runner_model="new:1b")

    after = store.spec_path(home, "T-1").read_text()
    assert "runner: opencode" in after
    assert "runner_model: new:1b" in after
    assert "old:1b" not in after


def test_set_runner_appends_no_event(home, cfg):
    _seed_ticket(home, "T-1")
    before = event_log.read(home, "T-1")

    ops.set_runner(cfg, "T-1", runner="opencode")

    assert event_log.read(home, "T-1") == before


def test_set_runner_makes_the_ticket_due_next_sweep_via_spec_hash_mismatch(home, cfg):
    """The mechanism the docstring claims: a spec-hash mismatch alone -- no
    event needed -- is what wakes the ticket (`dispatcher.is_due`'s
    "spec-changed" branch), the same as any other direct human spec edit."""
    _seed_ticket(home, "T-1", phase=Phase.TRIAGING)
    ops.observe_spec(cfg, "T-1")  # records the pre-edit hash

    ops.set_runner(cfg, "T-1", runner="opencode")

    snap = snap_mod.load(home, "T-1")
    current = disp.spec_hash_on_disk(home, "T-1")
    due = disp.is_due(home, "T-1", snap, inbox_pending=False, current_spec_hash=current, now=1000)
    assert due.due is True
    assert due.reason == "spec-changed"


def test_set_runner_allows_once_implementing_with_no_live_claim(home, cfg):
    """T-54: `implementing` alone no longer blocks an edit -- see
    `gates.runner_editable`'s generalization."""
    _seed_ticket(home, "T-1", phase=Phase.IMPLEMENTING)

    result = ops.set_runner(cfg, "T-1", runner="opencode")

    assert result["runner"] == "opencode"
    assert "runner: opencode" in store.spec_path(home, "T-1").read_text()


def test_set_runner_allows_with_worktree_present_and_no_live_claim(home, cfg):
    """T-54: worktree existence alone no longer blocks an edit -- see
    `gates.runner_editable`'s generalization."""
    _seed_ticket(home, "T-1", phase=Phase.READY)
    store.worktree_path(home, "T-1").mkdir(parents=True)

    result = ops.set_runner(cfg, "T-1", runner="opencode")

    assert result["runner"] == "opencode"


def test_set_runner_rejects_with_confirmed_live_claim_appends_no_event(home, cfg, child_process):
    _seed_ticket(home, "T-1", phase=Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    before_events = event_log.read(home, "T-1")
    before_spec = store.spec_path(home, "T-1").read_bytes()

    with pytest.raises(store.MaestroError):
        ops.set_runner(cfg, "T-1", runner="opencode")

    assert event_log.read(home, "T-1") == before_events
    assert store.spec_path(home, "T-1").read_bytes() == before_spec


def test_set_runner_requires_at_least_one_field(home, cfg):
    _seed_ticket(home, "T-1")
    with pytest.raises(store.MaestroError):
        ops.set_runner(cfg, "T-1")


def test_set_runner_rejects_unknown_key(home, cfg):
    with pytest.raises(store.MaestroError):
        ops.set_runner(cfg, "NOPE", runner="opencode")


def test_set_runner_warns_but_succeeds_on_unreachable_daemon(home, cfg, monkeypatch):
    _seed_ticket(home, "T-1")
    monkeypatch.setattr("maestro.providers.ollama.fetch_models",
                        lambda *a, **kw: (None, "refused"))

    result = ops.set_runner(cfg, "T-1", runner="opencode", runner_model="qwen3-coder:30b")

    assert result["runner_model"] == "qwen3-coder:30b"  # stored verbatim -- never gated
    assert "unreachable" in result["warning"]


def test_set_runner_warns_but_succeeds_on_missing_model(home, cfg, monkeypatch):
    _seed_ticket(home, "T-1")
    monkeypatch.setattr(
        "maestro.providers.ollama.fetch_models",
        lambda *a, **kw: ([{"name": "llama3:8b", "capabilities": ["tools"]}], None))

    result = ops.set_runner(cfg, "T-1", runner="opencode", runner_model="not-installed:1b")

    assert result["runner_model"] == "not-installed:1b"
    assert "llama3:8b" in result["warning"]


def test_set_runner_never_warns_for_claude(home, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "maestro.providers.ollama.fetch_models",
        lambda *a, **kw: calls.append(1) or ([], None))
    _seed_ticket(home, "T-1")

    result = ops.set_runner(cfg, "T-1", runner="claude")

    assert calls == []
    assert result["warning"] is None


# --- T-61 (PI-9): a pi runner_model validates against pi's own catalogue --------

def test_set_runner_pi_warns_from_pis_own_catalogue_never_touches_ollama(home, cfg, monkeypatch):
    _seed_ticket(home, "T-1")
    ollama_calls = []
    monkeypatch.setattr(
        "maestro.providers.ollama.fetch_models",
        lambda *a, **kw: ollama_calls.append(1) or ([], None))
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))

    result = ops.set_runner(cfg, "T-1", runner="pi", runner_model="glm-9.9-does-not-exist")

    assert result["runner_model"] == "glm-9.9-does-not-exist"  # stored verbatim -- never gated
    assert "glm-5.2" in result["warning"]
    assert ollama_calls == []


def test_set_runner_pi_warns_but_succeeds_when_pi_unreachable(home, cfg, monkeypatch):
    _seed_ticket(home, "T-1")
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: (None, "pi --list-models timed out after 15.0s"))

    result = ops.set_runner(cfg, "T-1", runner="pi", runner_model="glm-5.2")

    assert result["runner_model"] == "glm-5.2"
    assert "pi unreachable" in result["warning"]
    assert "timed out" in result["warning"]


def test_set_runner_pi_ok_when_model_present(home, cfg, monkeypatch):
    _seed_ticket(home, "T-1")
    monkeypatch.setattr(
        "maestro.providers.pi.fetch_models",
        lambda *a, **kw: ([{"provider": "zai", "model": "glm-5.2", "context": "1.0M",
                            "max-out": "128K", "thinking": "yes", "images": "no"}], None))

    result = ops.set_runner(cfg, "T-1", runner="pi", runner_model="glm-5.2")

    assert result["warning"] is None


# --- CLI `maestro runner <KEY>` -------------------------------------------------

def test_cli_runner_rewrites_only_the_new_lines(home):
    _seed_ticket(home, "T-1")
    spec_path = store.spec_path(home, "T-1")
    before = spec_path.read_text()

    rc = cli_main(["--home", str(home), "runner", "T-1",
                   "--runner", "opencode", "--runner-model", "qwen3-coder:30b"])

    assert rc == 0
    after = spec_path.read_text()
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
    added = [l for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
    assert added == ["+runner: opencode", "+runner_model: qwen3-coder:30b"]
    assert removed == []


def test_cli_runner_with_confirmed_live_claim_exits_nonzero_appends_no_event(
        home, capsys, child_process):
    """AC6/AC7 (T-54): refused with the shared message, and no event/spec byte
    changes, when a reconciler session for the key is actually in flight
    (`implementing` alone no longer refuses -- see `gates.runner_editable`'s
    generalization)."""
    _seed_ticket(home, "T-1", phase=Phase.IMPLEMENTING)
    claims.write_claim(home, "T-1", child_process.pid, "reconcile-T-1")
    before_events = event_log.read(home, "T-1")
    before_spec = store.spec_path(home, "T-1").read_bytes()

    rc = cli_main(["--home", str(home), "runner", "T-1", "--runner", "opencode"])

    assert rc != 0
    assert event_log.read(home, "T-1") == before_events
    assert store.spec_path(home, "T-1").read_bytes() == before_spec
    err = capsys.readouterr().err
    assert "error:" in err


def test_cli_runner_requires_at_least_one_flag(home):
    _seed_ticket(home, "T-1")
    rc = cli_main(["--home", str(home), "runner", "T-1"])
    assert rc != 0


def test_cli_runner_prints_warning_to_stderr(home, monkeypatch, capsys):
    _seed_ticket(home, "T-1")
    monkeypatch.setattr("maestro.providers.ollama.fetch_models",
                        lambda *a, **kw: (None, "refused"))

    rc = cli_main(["--home", str(home), "runner", "T-1",
                   "--runner", "opencode", "--runner-model", "qwen3-coder:30b"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err
    assert "unreachable" in err


def test_cli_runner_pi_prints_warning_sourced_from_pi_not_ollama(home, monkeypatch, capsys):
    """T-61 (PI-9): the real `maestro runner <KEY>` CLI verb, sourced from
    pi's own catalogue -- ollama is never touched for a pi runner_model."""
    _seed_ticket(home, "T-1")
    ollama_calls = []
    monkeypatch.setattr("maestro.providers.ollama.fetch_models",
                        lambda *a, **kw: ollama_calls.append(1) or ([], None))
    monkeypatch.setattr("maestro.providers.pi.fetch_models",
                        lambda *a, **kw: (None, "no such file: pi"))

    rc = cli_main(["--home", str(home), "runner", "T-1",
                   "--runner", "pi", "--runner-model", "glm-5.2"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err
    assert "pi unreachable" in err
    assert ollama_calls == []


# --- AC8: `runner` is a human-only verb, never granted to agents ---------------

def test_runner_verb_absent_from_agent_tool_verbs():
    from maestro.cli import _AGENT_TOOL_VERBS
    assert "runner" not in _AGENT_TOOL_VERBS
    assert "runner" not in disp.AGENT_TOOL_VERBS
