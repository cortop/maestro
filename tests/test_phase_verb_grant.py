"""RB-16: every spawned reconciler is granted only the maestro verbs its
phase's own skill file actually invokes, not the flat ~23-verb set every
phase got before this ticket (`dispatcher.phase_verb_grant`/
`phase_verb_denylist`, resolved per key at the same spawn site as
`phase_denylist`/`resolved_allowed_tools`).

Real-surface QA (AD-4, CLAUDE.md's own QA convention): every argv-shaped
assertion below drives the real `dispatcher.dispatch()` sweep over a temp
home with `DryRunSessions` -- the sanctioned mock boundary is the `claude -p`
spawn itself, never the grant-resolution logic under test -- and inspects the
actually-recorded --allowedTools/--disallowedTools argv, exactly what a live
dispatcher sweep would hand a real `claude -p` invocation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from maestro import dispatcher as disp, inbox
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import seed_ticket

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every verb `dispatcher.AGENT_TOOL_VERBS` recognizes, longest-first so e.g.
# "set-phase" can never be shadowed by a shorter unrelated prefix match (none
# currently collide -- this just stays correct if one ever does).
_ALL_VERBS = sorted(disp.AGENT_TOOL_VERBS, key=len, reverse=True)
_VERB_RE = re.compile(r"\bmaestro (" + "|".join(re.escape(v) for v in _ALL_VERBS) + r")\b")

# suffix -> a Phase that resolves to it, for functions keyed by Phase
# (phase_verb_grant/phase_verb_denylist) rather than by suffix directly.
_PHASE_BY_SUFFIX = {suffix: phase for phase, suffix in disp._PHASE_COMMAND_SUFFIX.items()}


def _verbs_invoked_by_skill(suffix: str) -> set[str]:
    path = REPO_ROOT / "skills" / f"maestro-reconcile-{suffix}.md"
    text = path.read_text(encoding="utf-8")
    return {m.group(1) for m in _VERB_RE.finditer(text)}


def _granted_verbs(suffix: str) -> set[str]:
    return set(disp._PHASE_VERB_GRANT_BY_SUFFIX[suffix])


# ---------------------------------------------------------------------------
# AC4: the per-phase set is derived from each phase's own skill file, or a
# drift test fails when a skill references a verb its phase isn't granted.
# Runtime-parsing the markdown itself (rather than hand-authoring
# `_PHASE_VERB_GRANT_BY_SUFFIX` once, the way `_PHASE_COMMAND_SUFFIX`/
# `PHASE_CLASS` are already hand-authored) would tie the grant to one
# specific install layout -- maestro is project-agnostic, a bound repo's own
# skills may not live at this path -- so this is the sanctioned fallback
# (RB-16 spec Notes): a hand table plus a test that fails the instant it
# drifts from what the skill files actually call.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", sorted(disp._PHASE_VERB_GRANT_BY_SUFFIX))
def test_phase_verb_grant_covers_every_verb_the_skill_actually_calls(suffix):
    invoked = _verbs_invoked_by_skill(suffix)
    missing = invoked - _granted_verbs(suffix)
    assert not missing, (
        f"skills/maestro-reconcile-{suffix}.md calls maestro {sorted(missing)} but "
        f"dispatcher._PHASE_VERB_GRANT_BY_SUFFIX[{suffix!r}] doesn't grant it -- "
        "a real reconciler in this phase would be silently blocked")


def test_every_suffix_names_a_real_skill_file():
    """Catches a renamed/deleted skill file the table wasn't updated for."""
    for suffix in disp._PHASE_VERB_GRANT_BY_SUFFIX:
        assert (REPO_ROOT / "skills" / f"maestro-reconcile-{suffix}.md").exists()


def test_every_phase_command_suffix_has_a_verb_grant_row():
    """Every suffix `resolve_reconcile_command` can actually route to (i.e.
    every phase, since an unrecognized one falls back to "passive") has a
    matching `_PHASE_VERB_GRANT_BY_SUFFIX` row -- no phase silently falls
    through to a KeyError at spawn time."""
    for suffix in disp._PHASE_COMMAND_SUFFIX.values():
        assert suffix in disp._PHASE_VERB_GRANT_BY_SUFFIX
    assert "passive" in disp._PHASE_VERB_GRANT_BY_SUFFIX  # the try/except fallback target too


# ---------------------------------------------------------------------------
# AC2: grant ∪ deny must equal the full AGENT_TOOL_VERBS ceiling, exactly
# once each -- narrowing must never silently drop a verb from BOTH lists, or
# grant one outside the pre-existing ceiling.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", sorted(disp._PHASE_VERB_GRANT_BY_SUFFIX))
def test_phase_grant_and_denylist_partition_agent_tool_verbs(suffix):
    granted = _granted_verbs(suffix)
    phase = _PHASE_BY_SUFFIX[suffix]
    denied_rules = disp.phase_verb_denylist(phase.value)
    denied = {r.removeprefix("Bash(maestro ").removesuffix(":*)") for r in denied_rules}
    assert granted & denied == set()
    assert granted | denied == set(disp.AGENT_TOOL_VERBS)


def test_maestro_coarse_grant_constant_unmoved():
    """The module-load-time guard `phase_verb_denylist`'s docstring cites --
    still true, still checkable without importing cli.py."""
    assert disp.RECONCILER_REQUIRED_TOOLS[0] == disp.MAESTRO_COARSE_GRANT == "Bash(maestro:*)"


# ---------------------------------------------------------------------------
# AC1 / AC5: a real spawn's recorded argv carries a phase-appropriate verb
# set -- driven through the real `dispatcher.dispatch()` sweep, one full
# sweep per phase over a temp home, never a mock below the claude -p
# boundary.
# ---------------------------------------------------------------------------

def _spawned_tools(cfg, key):
    sessions = DryRunSessions()
    report = disp.dispatch(cfg, sessions, now=1000)
    assert key in report.spawned, f"{key} did not spawn this sweep -- due was {report.due}"
    match = next(s for s in sessions.spawned if s[0] == key)
    _key, _prompt, _cwd, _model, _effort, disallowed, allowed = match[:7]
    return set(allowed), set(disallowed)


def test_qa_spawn_cannot_call_finalize(home, cfg):
    seed_ticket(home, "T-1", "t", phase=Phase.QA.value)
    allowed, disallowed = _spawned_tools(cfg, "T-1")
    assert "Bash(maestro finalize:*)" not in allowed
    assert "Bash(maestro finalize:*)" in disallowed  # explicit deny, not mere omission


def test_triaging_spawn_cannot_call_verify_ac(home, cfg):
    seed_ticket(home, "T-1", "t")  # TicketCreated alone -> triaging
    allowed, disallowed = _spawned_tools(cfg, "T-1")
    assert "Bash(maestro verify-ac:*)" not in allowed
    assert "Bash(maestro verify-ac:*)" in disallowed


def test_terminating_spawn_cannot_call_impl_turn(home, cfg):
    seed_ticket(home, "T-1", "t", phase=Phase.TERMINATING.value)
    allowed, disallowed = _spawned_tools(cfg, "T-1")
    assert "Bash(maestro impl-turn:*)" not in allowed
    assert "Bash(maestro impl-turn:*)" in disallowed


@pytest.mark.parametrize("phase,suffix", [
    (Phase.TRIAGING, "triaging"),
    (Phase.READY, "ready"),
    (Phase.RESEARCHING, "researching"),
    (Phase.IMPLEMENTING, "implementing"),
    (Phase.QA, "qa"),
    # awaiting-ci/in-review/degraded share this exact row -- resolved by
    # suffix, not by phase (see `_phase_verb_suffix`) -- so exercising one
    # "passive"-suffix phase end-to-end proves it for all four at once.
    (Phase.TERMINATING, "passive"),
])
def test_full_sweep_spawn_argv_matches_phase_grant_exactly(home, cfg, phase, suffix):
    seed_ticket(home, "T-1", "t", phase=phase.value)
    allowed, disallowed = _spawned_tools(cfg, "T-1")

    expected_allowed = (set(disp.phase_verb_grant(phase.value))
                        # ...plus the ONE reconcile command this spawn invokes,
                        # so the Skill call that loads it is not denied. Still
                        # exact: no other phase's skill, and never bare `Skill`.
                        | set(disp.skill_grant(f"/maestro-reconcile-{suffix}")))
    expected_denied = ({"Bash(gh pr merge:*)"} | set(disp.phase_denylist(phase.value))
                        | set(disp.phase_verb_denylist(phase.value)))
    assert allowed == expected_allowed
    assert disallowed == expected_denied

    # No verb this phase's own skill file actually calls is missing from the
    # grant it spawns under (AC5: no session blocked on a missing verb).
    granted_verbs = {r.removeprefix("Bash(maestro ").removesuffix(":*)") for r in allowed}
    assert _verbs_invoked_by_skill(suffix) <= granted_verbs


def test_awaiting_human_spawn_grants_only_its_own_verbs(home, cfg):
    """The one remaining phase above's parametrize list skips: it's a
    SLEEPING phase (`statemachine.PHASE_CLASS`), so it only spawns when
    forced due -- here, via a pending inbox command, the real wake signal a
    sleeping phase gets (same technique test_web_tools.py's own nudge test
    uses)."""
    seed_ticket(home, "T-1", "t", phase=Phase.AWAITING_HUMAN.value,
                questions={"q1": "ok?"})
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})
    allowed, disallowed = _spawned_tools(cfg, "T-1")
    assert allowed == (set(disp.phase_verb_grant(Phase.AWAITING_HUMAN.value))
                       | {"Skill(maestro-reconcile-awaiting-human)"})
    assert "Bash(maestro finalize:*)" in allowed       # research-ticket mint path needs it
    assert "Bash(maestro verify-ac:*)" not in allowed
    assert "Bash(maestro verify-ac:*)" in disallowed


# ---------------------------------------------------------------------------
# AC3: the coarse `Bash(maestro:*)` wildcard (.claude/settings.json, humans)
# and the per-phase grant (agent spawns) are two independent, decided
# mechanisms -- the coarse one stays untouched, but an agent spawn always
# gets the narrower grant enforced via an explicit deny, so the coarse
# wildcard can't silently reopen it for a spawned reconciler (Claude Code
# lets an explicit deny win over any allow -- the same precedent
# `phase_denylist`'s Edit/Write block for `qa` already relies on).
# ---------------------------------------------------------------------------

def test_coarse_grant_still_present_untouched_in_dev_settings():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(maestro:*)" in settings["permissions"]["allow"]


def test_out_of_phase_verb_explicitly_denied_despite_repos_own_coarse_grant(home, cfg):
    """This repo's own .claude/settings.json (asserted above) grants the
    coarse wildcard for humans -- prove an agent-spawned qa session here
    still gets an EXPLICIT deny for a verb outside its phase, not just an
    omission a coarser repo-level allow could otherwise reopen."""
    seed_ticket(home, "T-1", "t", phase=Phase.QA.value)
    _allowed, disallowed = _spawned_tools(cfg, "T-1")
    assert "Bash(maestro finalize:*)" in disallowed


# --- the Skill grant -----------------------------------------------------------
#
# A reconciler is told to load `/maestro-reconcile-<phase>` and reaches for the
# `Skill` tool. Those files are installed as slash COMMANDS, never as registry
# skills, and a Skill call whose target is a command is permission-gated --
# auto-declined in headless `-p`. Measured: 249 sessions, one denial each, 142
# on the session's literal first turn. The reconciler recovers, so the cost is
# a burnt turn per session, every session, every phase.


@pytest.mark.parametrize("phase,suffix", [
    (Phase.READY, "ready"),
    (Phase.IMPLEMENTING, "implementing"),
    (Phase.QA, "qa"),
    (Phase.RESEARCHING, "researching"),
])
def test_every_phase_spawn_grants_its_own_reconcile_command(home, cfg, phase, suffix):
    """Real sweep, real argv: the grant names the command actually resolved."""
    seed_ticket(home, "T-1", "t", phase=phase.value)
    allowed, _disallowed = _spawned_tools(cfg, "T-1")
    # Exactly one Skill rule, for this phase's own command, never bare `Skill`.
    assert {r for r in allowed if r.startswith("Skill")} == {
        f"Skill(maestro-reconcile-{suffix})"}


def test_the_grant_is_never_the_bare_skill_rule():
    """A bare `Skill` would admit every skill and slash command resolvable from
    the worktree -- 80 and 142 in a dd-source checkout, including ones that
    delete caches or implement tickets. Same self-widening AD-1 forbids for
    `Bash(maestro:*)`."""
    for command in ("/maestro-reconcile-passive", "/post-qa-polish",
                    "maestro-reconcile-qa"):
        rules = disp.skill_grant(command)
        assert len(rules) == 1
        assert rules[0] != "Skill"
        assert rules[0].startswith("Skill(") and rules[0].endswith(")")
        assert "*" not in rules[0]
    assert disp.skill_grant("") == []


def test_grant_is_phase_scoped_not_the_whole_reconcile_family():
    """One spawn must not be able to invoke another phase's skill."""
    granted = disp.skill_grant("/maestro-reconcile-qa")
    for other in ("implementing", "passive", "ready", "triaging"):
        assert f"Skill(maestro-reconcile-{other})" not in granted
