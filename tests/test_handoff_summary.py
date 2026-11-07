"""`maestro handoff --summary` records the agent's own round summary in the PhaseChanged reason."""
from __future__ import annotations

import re
from pathlib import Path

from maestro import cli, event_log, snapshot as snap_mod

from conftest import seed_ticket

KEY = "T-1"
ROOT = Path(__file__).resolve().parent.parent


def _seed(home):
    seed_ticket(home, KEY, "handoff", phase="ready", pr=7)
    event_log.append(home, KEY, "PhaseChanged", {"phase": "implementing", "reason": "worktree ready"}, actor="r")
    snap_mod.rebuild(home, KEY)


def _last_reason(home):
    evs = [e for e in event_log.read(home, KEY) if e["type"] == "PhaseChanged"]
    return evs[-1]["payload"]["reason"]


def test_summary_is_appended_with_newlines_collapsed(home):
    _seed(home)
    assert cli.main(["--home", str(home), "handoff", KEY, "--summary", "added X\nand Y\n\n  because Z"]) == 0
    assert _last_reason(home) == "fresh round done: added X and Y because Z"


def test_without_summary_reason_is_unchanged(home):
    _seed(home)
    assert cli.main(["--home", str(home), "handoff", KEY]) == 0
    assert _last_reason(home) == "fresh round done"


def test_blank_summary_is_ignored(home):
    _seed(home)
    assert cli.main(["--home", str(home), "handoff", KEY, "--summary", " \n "]) == 0
    assert _last_reason(home) == "fresh round done"


def test_summary_reaches_context_dossier(home):
    _seed(home)
    assert cli.main(["--home", str(home), "handoff", KEY, "--summary", "synthetic summary text"]) == 0
    dossier = home / "derived" / "context" / f"{KEY}.md"
    assert "fresh round done: synthetic summary text" in dossier.read_text()


def test_every_round_handoff_line_passes_summary():
    files = sorted((ROOT / "maestro" / "_implementing_rounds").glob("*.md"))
    seen = 0
    for f in files:
        for line in f.read_text().splitlines():
            if re.search(r"maestro handoff\b", line):
                seen += 1
                assert '--summary "<what you did, what you addressed or declined, and why>"' in line, (f.name, line)
    assert seen >= len(files)
