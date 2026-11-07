"""The `Step:` narration convention in the reconcile skills and its extractor."""
from pathlib import Path

import pytest

from maestro import steplog

SKILLS = Path(__file__).resolve().parent.parent / ".claude" / "commands"
PHASES = ["implementing", "qa", "triaging", "researching", "awaiting-human"]


@pytest.mark.parametrize("phase", PHASES)
def test_skill_has_step_convention(phase):
    text = (SKILLS / f"maestro-reconcile-{phase}.md").read_text()
    assert "`Step: <what you are doing, present continuous, at most 8 words>`" in text


def test_step_lines_in_order_line_start_only():
    text = "Step: Reading the spec\nnotes Step: not a step\n  Step: indented\nStep: Writing tests\n"
    assert steplog.step_lines(text) == ["Reading the spec", "Writing tests"]


def test_claude_and_pi_records():
    claude = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Step: Adding the helper"},
        {"type": "tool_use", "name": "Bash", "input": {}},
        {"type": "text", "text": "done\nStep: Running tests"}]}}
    pi = {"type": "message_end", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Step: Fixing a failure"}]}}
    assert steplog.record_steps(claude) == ["Adding the helper", "Running tests"]
    assert steplog.record_steps(pi) == ["Fixing a failure"]
    assert steplog.record_steps({"type": "message_end", "message": {"role": "user"}}) == []


def test_cap_and_empty():
    long = "Step: " + "x" * 200
    assert steplog.step_lines(long) == ["x" * steplog.STEP_MAX_CHARS]
    assert steplog.step_lines("Step:\nStep:   \n") == []
