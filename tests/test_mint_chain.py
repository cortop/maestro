"""MTO-3: the research->implementation mint chain, proven against the real CLI.

`maestro create --title` never existed -- `maestro-reconcile-awaiting-human.md`'s
`## awaiting-human` branch emitted exactly that (a `--title` flag on a subparser
whose title was positional-only), so it failed with "unrecognized arguments:
--title" every time a research ticket's proposal was approved. Reported and
re-verified four times because nothing parsed the literal command the skill
emits -- test_reconcile_skill.py::test_every_maestro_invocation_in_every_phase_skill_parses
now catches a future drift of this shape statically; this file proves the fixed
command actually runs, end to end, over a real temp MAESTRO_HOME.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from maestro import inbox
from maestro.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parents[1]
AWAITING_HUMAN_SKILL = REPO_ROOT / ".claude" / "commands" / "maestro-reconcile-awaiting-human.md"


def _extract_create_argv(key: str, title: str, intent: str, notes: str) -> list[str]:
    """Pull the literal `maestro create ...` invocation straight out of the
    skill file (not a re-typed copy of it) and substitute its placeholders --
    so a future edit to the skill's flags is exercised here unmodified, the
    way MTO-3's `--title` bug never was."""
    text = AWAITING_HUMAN_SKILL.read_text()
    m = re.search(r"```bash\n(maestro create .*?)\n```", text, re.DOTALL)
    assert m, "could not find the `maestro create` fence in the awaiting-human skill"
    fence = m.group(1)
    # Join the backslash-continued physical lines into one logical command.
    command = " ".join(line.rstrip("\\").strip() for line in fence.splitlines())
    command = (
        command
        .replace('"Implement: <research-title-without-Research-prefix>"', f'"{title}"')
        .replace('"<chosen approach text>"', f'"{intent}"')
        .replace(
            '"Seeded from $KEY proposal. See tickets/$KEY/proposal.md for full context."',
            f'"{notes}"',
        )
        .replace('"$KEY"', f'"{key}"')
    )
    tokens = shlex.split(command)
    assert tokens[0] == "maestro" and tokens[1] == "create", (
        f"expected the fence to start with `maestro create`, got: {command!r}")
    return tokens[1:]  # keep "create" -- it's the subcommand argv, not maestro's own argv


def test_awaiting_human_create_command_parses_and_queues_a_ticket(home):
    """AC1: the exact command emitted by maestro-reconcile-awaiting-human.md
    parses and creates a ticket -- via the real CLI over a temp MAESTRO_HOME."""
    argv = _extract_create_argv(
        key="R-1",
        title="Implement: caching strategies",
        intent="Use an LRU cache with TTL expiry backed by a Redis sidecar.",
        notes="Seeded from R-1 proposal. See tickets/R-1/proposal.md for full context.",
    )
    rc = cli_main(["--home", str(home), *argv])
    assert rc == 0, f"`maestro create {' '.join(argv)}` failed to parse/run (rc={rc})"

    pending = inbox.pending_new(home)
    assert len(pending) == 1
    _, entry = pending[0]
    assert entry["title"] == "Implement: caching strategies"
    assert entry["args"]["kind"] == "implementation"
    assert entry["args"]["approval_tier"] == 0
    assert entry["args"]["depends_on"] == ["R-1"]
    assert "LRU cache" in entry["args"]["intent"]


def test_create_title_flag_and_positional_both_work(home):
    """The CLI-side half of the fix: `--title` is now a real alias for the
    positional title, so any already-distributed copy of the old skill text
    (or a hand-typed `--title` invocation) still works."""
    assert cli_main(["--home", str(home), "create", "Positional title",
                     "--key", "P-1", "--no-nudge"]) == 0
    assert cli_main(["--home", str(home), "create", "--title", "Flag title",
                     "--key", "P-2", "--no-nudge"]) == 0

    pending = {e["key"]: e for _, e in inbox.pending_new(home)}
    assert pending["P-1"]["title"] == "Positional title"
    assert pending["P-2"]["title"] == "Flag title"


def test_create_rejects_title_given_both_ways(home):
    """Ambiguous input (both the positional and --title) is a usage error,
    not a silent pick-one."""
    import io
    import contextlib

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = cli_main(["--home", str(home), "create", "Positional",
                       "--title", "Flag", "--key", "X-1", "--no-nudge"])
    assert rc != 0
    assert inbox.pending_new(home) == []
