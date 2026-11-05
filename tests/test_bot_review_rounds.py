"""The implementing skill keeps bot-only review rounds cheap: sync only when needed, decline optional suggestions."""
import re

from maestro import implementing_brief


def _text() -> str:
    return re.sub(r"\s+", " ", implementing_brief.instructions("review-feedback"))


def test_sync_only_when_needed():
    text = re.sub(r"\s+", " ", implementing_brief.instructions("review-feedback"))
    assert "A review reason alone is not a reason to sync with base" in text
    assert "`CONFLICTING`" in text and "drift policy requires it" in text


def test_decline_nonblocking_bot_suggestion():
    text = _text()
    assert "Non-blocking AI-reviewer suggestions are optional" in text
    assert "you may decline it" in text and "push nothing" in text
    assert "Never decline this way a `changes requested:` review or any comment written by a human" in text
    assert "a bot" not in text.lower().split("non-blocking ai-reviewer")[1][:600]
