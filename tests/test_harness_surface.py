"""Prompt hygiene for the reconcile skills: a per-file byte ceiling and a
check that every `/name` a skill mentions is loadable in its phase.

Pure file reads over `.claude/commands/`; no home or sweep is involved.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from maestro import dispatcher as disp

COMMANDS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "commands"

# Byte ceiling per skill: its size when pinned plus small headroom. Growing a
# skill past its ceiling is a reviewed edit to this table, so cut something else
# to pay for always-on text first.
SKILL_BYTE_CEILINGS = {
    "awaiting-human": 7000,
    "implementing": 33400,
    "passive": 6600,
    "qa": 7300,
    "ready": 5800,
    "researching": 7500,
    "triaging": 7100,
}

# suffix -> extra `/name` references that phase's skill may mention. The
# dispatcher's `Skill(...)` grant admits only the phase's own command, so an
# entry here must be backed by a matching grant.
EXPLICIT_SKILL_GRANTS: dict[str, set[str]] = {}

# A `/name` at the start of a line, after whitespace or `(`, optionally opening
# a backtick span. Mid-word slashes ("`discard`/rejection") and paths don't match.
_SKILL_REF_RE = re.compile(r"(?:^|[\s(])`?(/[a-z][a-z0-9-]*)(?![\w/.-])", re.MULTILINE)


def _skill_files() -> list[Path]:
    return sorted(COMMANDS_DIR.glob("maestro-reconcile-*.md"))


def _suffix(path: Path) -> str:
    return path.stem.removeprefix("maestro-reconcile-")


def test_skill_byte_ratchet():
    suffixes = {_suffix(p) for p in _skill_files()}
    assert suffixes == set(SKILL_BYTE_CEILINGS), (
        "SKILL_BYTE_CEILINGS must name exactly the reconcile skills: "
        f"{sorted(suffixes ^ set(SKILL_BYTE_CEILINGS))}")
    for path in _skill_files():
        size = path.stat().st_size
        ceiling = SKILL_BYTE_CEILINGS[_suffix(path)]
        assert size <= ceiling, (
            f"{path.name} is {size} bytes, over its {ceiling} ceiling -- cut text "
            "or raise the ceiling in a reviewed edit")


@pytest.mark.parametrize("path", _skill_files(), ids=_suffix)
def test_skill_references_are_granted(path):
    suffix = _suffix(path)
    allowed = {f"/maestro-reconcile-{suffix}"} | EXPLICIT_SKILL_GRANTS.get(suffix, set())
    refs = set(_SKILL_REF_RE.findall(path.read_text(encoding="utf-8")))
    assert refs <= allowed, (
        f"{path.name} references {sorted(refs - allowed)}, which its phase can't load")


def test_skill_ref_regex_ignores_prose_slashes():
    assert _SKILL_REF_RE.findall("a `discard`/rejection and src/foo and `/deep-research` x") == [
        "/deep-research"]


def test_own_command_matches_dispatcher_suffixes():
    assert {_suffix(p) for p in _skill_files()} == set(disp._PHASE_COMMAND_SUFFIX.values())
