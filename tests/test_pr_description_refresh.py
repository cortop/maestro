"""T-216: the implementing skill refreshes an open PR's title/body after a code push."""
from maestro import dispatcher as disp
from maestro import implementing_brief

def _round(name: str) -> str:
    """One implementing round's instructions, whitespace-normalised."""
    return " ".join(implementing_brief.instructions(name).split())


REFRESH_ROUNDS = ("qa-fix", "ci-fix", "review-feedback")


def test_refresh_step_compares_and_edits_after_push():
    for name in REFRESH_ROUNDS:
        sec = _round(name)
        assert "--json title,body" in sec and "git -C <WT> diff" in sec, name
        assert "gh pr edit" in sec, name


def test_refresh_keeps_human_text_and_rebuilds_ac_table_from_ac_verified():
    for name in REFRESH_ROUNDS:
        sec = _round(name)
        assert "keep human-written lines verbatim" in sec, name
        assert "AC-to-evidence rows from `ac_verified`" in sec, name
        assert "only what the new commits made wrong" in sec, name


def test_refresh_skips_conflict_only_and_note_only_rounds():
    assert "skip it on a Note-only round" in _round("review-feedback")
    for name in ("conflict", "base-drift"):
        assert "gh pr edit" not in _round(name), name


def test_refresh_on_stack_is_per_entry():
    for name in REFRESH_ROUNDS:
        sec = _round(name)
        assert "only entries whose own diff changed" in sec, name
    assert "each from its own base...branch diff" in _round("qa-fix")


def test_gh_pr_edit_is_granted_and_not_denied():
    assert "Bash(gh:*)" in disp.RECONCILER_REQUIRED_TOOLS
    assert not any("gh pr edit" in d for d in disp.MERGE_DENYLIST)
