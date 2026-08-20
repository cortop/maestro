"""T-100: drift guards for the documentation surface itself -- the class of bug this
ticket exists to fix (T-81..T-94 changed config surface and reconciler behaviour, and
the docs were only updated where a ticket happened to touch them). Mirrors
tests/test_postmortem_drift.py's/tests/test_diagram.py's posture: fail `make test` the
moment a cited/counted artifact drifts from its source, instead of waiting for the next
audit to notice."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from maestro import config as config_mod, repos as repos_mod, skills_install

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXCLUDE_DIR_PARTS = {".venv", ".git", "node_modules", "__pycache__"}


def _tracked_markdown_files() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("*.md")
        if not any(part in _EXCLUDE_DIR_PARTS for part in p.relative_to(REPO_ROOT).parts)
    )


# ---------------------------------------------------------------------------
# No stale reconcile-payload file count: every docstring/help-string citation site
# names the count in English words, so a plain substring search can't just look for
# "7" -- this maps each site's own wording to an int and compares it against
# len(skills_install.PAYLOAD_NAMES), which is itself derived from PHASE_FILES. Add the
# 8th (or Nth) phase file, and this fails the moment a citation site wasn't updated to
# match -- it does not require guessing every possible English phrasing in advance,
# only the sites that actually exist today.
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_WORD_FOR_NUMBER = {v: k for k, v in _NUMBER_WORDS.items()}

_PAYLOAD_COUNT_CITATIONS = [
    (REPO_ROOT / "maestro" / "cli.py",
     re.compile(r"install(?:ed|s)? the (\w+) per-phase reconcile command files")),
    (REPO_ROOT / "maestro" / "cli.py",
     re.compile(r"install the (\w+) per-phase maestro-reconcile-\*\.md commands")),
    (REPO_ROOT / "maestro" / "skills_install.py",
     re.compile(r"copy of the (\w+) command files")),
    (REPO_ROOT / "maestro" / "skills_install.py",
     re.compile(r"[Cc]opy the (\w+) payload files")),
    (REPO_ROOT / "maestro" / "skills_install.py",
     re.compile(r"[Ss]ymlink the (\w+) payload files")),
    (REPO_ROOT / "DOGFOOD.md",
     re.compile(r"copies the (\w+) files into that")),
]


def test_no_stale_reconcile_payload_count_in_docs_help_or_docstrings():
    expected = len(skills_install.PAYLOAD_NAMES)
    assert expected in _WORD_FOR_NUMBER, (
        f"len(skills_install.PAYLOAD_NAMES) == {expected} has no entry in this test's "
        "_NUMBER_WORDS table -- extend it")
    expected_word = _WORD_FOR_NUMBER[expected]

    stale = []
    for path, pattern in _PAYLOAD_COUNT_CITATIONS:
        text = path.read_text(encoding="utf-8")
        m = pattern.search(text)
        assert m, (
            f"citation pattern {pattern.pattern!r} not found in {path} -- the wording "
            "moved; update _PAYLOAD_COUNT_CITATIONS to match")
        if m.group(1) != expected_word:
            stale.append(f"{path}: says {m.group(1)!r}, expected {expected_word!r}")
    assert not stale, (
        "stale reconcile-payload file count(s) -- update the citation(s), this is "
        f"exactly the T-100 drift class:\n" + "\n".join(stale))


# ---------------------------------------------------------------------------
# Every config knob T-81..T-94 introduced or materially changed the semantics of is
# mentioned by name in at least one tracked .md doc -- the load-bearing subset this
# ticket's spec Notes itemize (config.py §B), not the full ~70-field Config dataclass
# (most of which is intentionally documented only via its own config.toml comment, not
# prose .md). Each name is checked against the real dataclass fields first, so a rename
# fails loud here instead of the test silently checking a name nobody ships any more.
# ---------------------------------------------------------------------------

_RECENT_KNOBS = {
    "test_command": config_mod.Config,
    "test_deletion_gate": config_mod.Config,
    "worktree_timeout": config_mod.Config,
    "prime_timeout": config_mod.Config,
    "qa_phase_gate": config_mod.Config,
    "awaiting_ci_qa_gate": config_mod.Config,
    "qa_standards_axis": config_mod.Config,
    "language": repos_mod.RepoBinding,
}


def test_recent_config_knobs_still_resolve():
    for name, owner in _RECENT_KNOBS.items():
        field_names = {f.name for f in dataclasses.fields(owner)}
        assert name in field_names, (
            f"{name!r} is no longer a field of {owner.__name__} -- update "
            "_RECENT_KNOBS (rename or remove it) rather than leave a stale entry")


def test_recent_config_knobs_mentioned_in_tracked_markdown():
    combined = "\n".join(p.read_text(encoding="utf-8") for p in _tracked_markdown_files())
    missing = sorted(name for name in _RECENT_KNOBS if name not in combined)
    assert not missing, (
        "config knob(s) introduced/changed by T-81..T-94 aren't mentioned in any "
        f"tracked .md doc: {missing}")


# ---------------------------------------------------------------------------
# Every relative markdown link in the four hand-written top-level docs resolves to a
# file that actually exists -- a doc that links to a file that got renamed/removed is
# worse than no link at all (silently sends a reader into a 404).
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_LINKED_DOCS = ["README.md", "DESIGN.md", "CLAUDE.md", "DOGFOOD.md"]


def test_relative_markdown_links_resolve():
    broken = []
    for name in _LINKED_DOCS:
        path = REPO_ROOT / name
        text = path.read_text(encoding="utf-8")
        for target in _LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            if not resolved.exists():
                broken.append(f"{name}: [{target}] -> {resolved} does not exist")
    assert not broken, "broken relative markdown link(s):\n" + "\n".join(broken)


def test_linked_docs_actually_carry_at_least_one_relative_link():
    """Guards the guard above: if every one of these docs' links vanished, the previous
    test would trivially pass with nothing checked. At least one of the four must carry
    a real, resolving relative link (today: README/DESIGN both link
    docs/state-machine.md)."""
    found_any = False
    for name in _LINKED_DOCS:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for target in _LINK_RE.findall(text):
            if not target.startswith(("http://", "https://", "mailto:", "#")):
                found_any = True
    assert found_any, "none of README/DESIGN/CLAUDE/DOGFOOD carry a relative markdown link"
