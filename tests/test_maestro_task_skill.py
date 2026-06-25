"""Tests for the /maestro-task skill invariants.

The skill itself is a markdown prompt; these tests cover the underlying
behaviours it relies on so that regressions in the Python layer are caught
without needing an end-to-end LLM call.
"""
import re
from pathlib import Path

from maestro import dispatcher as disp, inbox, snapshot as snap_mod, store
from maestro.config import Config
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase


# ---------------------------------------------------------------------------
# AC-5: skill files are present in the tracked locations
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_skill_file_exists_in_skills_dir():
    """skills/maestro-task.md must be tracked so worktrees inherit it."""
    assert (REPO_ROOT / "skills" / "maestro-task.md").is_file()


def test_skill_file_exists_in_commands_dir():
    """.claude/commands/maestro-task.md must be present for user invocation."""
    assert (REPO_ROOT / ".claude" / "commands" / "maestro-task.md").is_file()


def test_skill_targets_dogfood_home():
    """The skill must reference the dogfood home, not the default ~/.maestro."""
    skill_text = (REPO_ROOT / "skills" / "maestro-task.md").read_text()
    assert "maestro-dev" in skill_text, "skill must reference the dogfood MAESTRO_HOME"
    assert "~/.maestro\n" not in skill_text and "~/.maestro'" not in skill_text, \
        "skill must not use bare ~/.maestro (wrong home)"


def test_skill_mentions_quality_rubric():
    """Skill must contain the quality rubric terms so the gate is enforced."""
    skill_text = (REPO_ROOT / "skills" / "maestro-task.md").read_text()
    for term in ("acceptance criteria", "clarifying questions", "rubric"):
        assert term in skill_text.lower(), f"quality rubric term missing: {term!r}"


# ---------------------------------------------------------------------------
# AC-2/3: dispatcher respects a pre-written spec.md (skill writes it first)
# ---------------------------------------------------------------------------

def test_dispatcher_does_not_overwrite_prewritten_spec(home, cfg):
    """When spec.md already exists before the dispatcher mints the ticket,
    the seed template must NOT overwrite it."""
    custom_spec = (
        "# T-1: add dark mode toggle\n\n"
        "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->\n\n"
        "approval_tier: 1\npriority: 2\n\n"
        "## Intent\nAdd a dark mode toggle to the settings panel.\n\n"
        "## Acceptance criteria\n- [ ] Toggle appears in settings and persists across reloads.\n"
    )
    store.spec_path(home, "T-1").parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write(store.spec_path(home, "T-1"), custom_spec)

    inbox.append_new(home, "add dark mode toggle", key="T-1")
    disp.dispatch(cfg, DryRunSessions(), now=1000)

    actual = store.spec_path(home, "T-1").read_text()
    assert actual == custom_spec, "dispatcher must not overwrite a pre-existing spec.md"


def test_prewritten_spec_phase_is_triaging_after_mint(home, cfg):
    """Even with a pre-written spec, the ticket must be minted in triaging."""
    store.spec_path(home, "T-1").parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write(
        store.spec_path(home, "T-1"),
        "# T-1: test\n\napproval_tier: 0\npriority: 1\n\n"
        "## Intent\nTest.\n\n## Acceptance criteria\n- [ ] It works.\n",
    )
    inbox.append_new(home, "test", key="T-1")
    disp.dispatch(cfg, DryRunSessions(), now=1000)
    assert snap_mod.load(home, "T-1").phase == Phase.TRIAGING.value


# ---------------------------------------------------------------------------
# Auto-key logic (the Python snippet the skill uses)
# ---------------------------------------------------------------------------

def _next_t_key(tickets_dir: Path) -> str:
    """Mirror of the auto-key logic in the skill."""
    nums = [
        int(m.group(1))
        for d in tickets_dir.iterdir()
        for m in [re.match(r"^T-(\d+)$", d.name)]
        if m
    ]
    return f"T-{max(nums, default=0) + 1}"


def test_auto_key_empty_board(home):
    assert _next_t_key(home / "tickets") == "T-1"


def test_auto_key_skips_existing(home):
    for n in (1, 2, 3):
        (home / "tickets" / f"T-{n}").mkdir()
    assert _next_t_key(home / "tickets") == "T-4"


def test_auto_key_ignores_non_t_keys(home):
    """Non-T-N keys (L-1, SK-1, …) must not affect T-N numbering."""
    for key in ("L-1", "SK-1", "TUI-9", "M-3"):
        (home / "tickets" / key).mkdir()
    assert _next_t_key(home / "tickets") == "T-1"


def test_auto_key_non_contiguous(home):
    for n in (1, 3, 5):
        (home / "tickets" / f"T-{n}").mkdir()
    assert _next_t_key(home / "tickets") == "T-6"


# ---------------------------------------------------------------------------
# Spec format validation helper (the skill must produce valid house format)
# ---------------------------------------------------------------------------

VALID_SPEC = """\
# T-42: add pagination to the status view

<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->

approval_tier: 1
priority: 2

## Intent
The status view currently dumps all tickets in one block. Adding pagination lets
users navigate large boards without scrolling through hundreds of lines.

## Acceptance criteria
- [ ] `maestro status` shows at most 20 rows by default.
- [ ] `--all` flag bypasses pagination and prints every ticket.
"""

MISSING_AC_SPEC = """\
# T-42: add pagination

<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->

approval_tier: 1
priority: 2

## Intent
Add pagination.

## Acceptance criteria
"""

VAGUE_AC_SPEC = """\
# T-42: add pagination

approval_tier: 1
priority: 2

## Intent
Improve the status view somehow.

## Acceptance criteria
- [ ] It works well.
"""


def _spec_passes_rubric(text: str) -> tuple[bool, str]:
    """Minimal rubric check that mirrors what the skill enforces."""
    has_intent = bool(re.search(r"## Intent\n(.+)", text, re.DOTALL))
    checkboxes = re.findall(r"- \[ \] .+", text)
    has_concrete_ac = any(
        len(cb.replace("- [ ] ", "").strip()) > 20 for cb in checkboxes
    )
    if not has_intent:
        return False, "missing intent"
    if not checkboxes:
        return False, "no acceptance criteria"
    if not has_concrete_ac:
        return False, "acceptance criteria too vague"
    return True, "ok"


def test_valid_spec_passes_rubric():
    ok, reason = _spec_passes_rubric(VALID_SPEC)
    assert ok, reason


def test_missing_ac_fails_rubric():
    ok, _ = _spec_passes_rubric(MISSING_AC_SPEC)
    assert not ok


def test_vague_ac_fails_rubric():
    ok, _ = _spec_passes_rubric(VAGUE_AC_SPEC)
    assert not ok
