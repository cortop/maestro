"""The implementing skill keeps bot-only review rounds cheap: sync only when needed, decline optional suggestions."""
import re
from pathlib import Path

SKILL = Path(__file__).parent.parent / ".claude/commands/maestro-reconcile-implementing.md"


def _text() -> str:
    return re.sub(r"\s+", " ", SKILL.read_text())


def test_sync_only_when_needed():
    text = _text()
    assert "sync with base only when needed" in text
    assert "`CONFLICTING`" in text and "drift policy requires a sync" in text
    assert "A review reason alone" in text and "is not a reason to sync" in text


def test_decline_nonblocking_bot_suggestion():
    text = _text()
    assert "Non-blocking AI-reviewer suggestions are optional" in text
    assert "you may decline it" in text and "push nothing" in text
    assert "Never decline this way a `changes requested:` review or any comment written by a human" in text
    assert "a bot" not in text.lower().split("non-blocking ai-reviewer")[1][:600]
