"""T-216: the implementing skill refreshes an open PR's title/body after a code push."""
from pathlib import Path

from maestro import dispatcher as disp

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "commands" / "maestro-reconcile-implementing.md"


def _section() -> str:
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    start = text.index("**Refresh the description after a code push")
    return text[start:text.index("**If this is a split-decision round", start)]


def test_refresh_step_compares_and_edits_after_push():
    sec = _section()
    assert "fix and review-feedback rounds only" in sec
    assert "--json title,body" in sec and "git -C <WT> diff" in sec
    assert "gh pr edit" in sec


def test_refresh_keeps_human_text_and_rebuilds_ac_table_from_ac_verified():
    sec = _section()
    assert "keep human-written lines verbatim" in sec
    assert "AC-to-evidence rows from `ac_verified`" in sec
    assert "only what the new commits made wrong" in sec


def test_refresh_skips_conflict_only_and_note_only_rounds():
    sec = _section()
    assert "skip conflict-only and Note-only rounds" in sec


def test_refresh_on_stack_is_per_entry():
    sec = _section()
    assert "only entries whose own diff changed" in sec
    assert "each from its own base...branch diff" in sec


def test_gh_pr_edit_is_granted_and_not_denied():
    assert "Bash(gh:*)" in disp.RECONCILER_REQUIRED_TOOLS
    assert not any("gh pr edit" in d for d in disp.MERGE_DENYLIST)
