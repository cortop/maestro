"""MTO-3: the research->implementation mint chain, proven against the real CLI.

`maestro create --title` never existed -- `maestro-reconcile-awaiting-human.md`'s
`## awaiting-human` branch emitted exactly that (a `--title` flag on a subparser
whose title was positional-only), so it failed with "unrecognized arguments:
--title" every time a research ticket's proposal was approved. Reported and
re-verified four times because nothing parsed the literal command the skill
emits -- test_reconcile_skill.py::test_every_maestro_invocation_in_every_phase_skill_parses
now catches a future drift of this shape statically; this file proves the fixed
command actually runs, end to end, over a real temp MAESTRO_HOME.

MTO-6: the skill's `maestro create` fence now also passes `--json`, so the mint
happens synchronously (via `dispatcher.mint_one`) instead of queuing into
`inbox/_new.jsonl` for the next dispatcher sweep -- the caller (the
awaiting-human reconciler) needs the assigned key back immediately, not on a
later sweep. `test_awaiting_human_create_command_parses_and_queues_a_ticket`
below is updated for that: it asserts the ticket is minted for real (event
log + snapshot), not just queued.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
from pathlib import Path

from maestro import inbox, snapshot as snap_mod
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


def test_awaiting_human_create_command_parses_and_queues_a_ticket(home, capsys):
    """AC1: the exact command emitted by maestro-reconcile-awaiting-human.md
    parses and creates a ticket -- via the real CLI over a temp MAESTRO_HOME.

    MTO-6: the fence carries `--json`, so this mints synchronously (bypassing
    `inbox/_new.jsonl` entirely) and prints the assigned key back -- proving
    the exact verb the skill now depends on to avoid the old python3 scrape."""
    argv = _extract_create_argv(
        key="R-1",
        title="Implement: caching strategies",
        intent="Use an LRU cache with TTL expiry backed by a Redis sidecar.",
        notes="Seeded from R-1 proposal. See tickets/R-1/proposal.md for full context.",
    )
    assert "--json" in argv, "expected the skill's create fence to mint synchronously via --json"

    rc = cli_main(["--home", str(home), *argv])
    assert rc == 0, f"`maestro create {' '.join(argv)}` failed to parse/run (rc={rc})"

    assert inbox.pending_new(home) == [], "synchronous --json mint must not queue into _new inbox"

    printed = json.loads(capsys.readouterr().out)
    impl_key = printed["key"]
    assert impl_key == "T-1"

    snap = snap_mod.load(home, impl_key)
    assert snap.title == "Implement: caching strategies"
    assert snap.kind == "implementation"
    assert "LRU cache" in (home / "tickets" / impl_key / "spec.md").read_text()


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


def test_create_json_mints_synchronously_with_an_explicit_key(home, capsys):
    """`create --json` with an explicit `--key` mints immediately -- no `_new`
    inbox round trip -- and echoes that same key back."""
    rc = cli_main(["--home", str(home), "create", "Explicit key ticket",
                   "--key", "Q-1", "--json", "--no-nudge"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"key": "Q-1"}
    assert inbox.pending_new(home) == []
    assert snap_mod.load(home, "Q-1").title == "Explicit key ticket"


def test_create_json_auto_key_mints_distinct_sequential_keys(home, capsys):
    """Two `create --json` calls with no `--key` each get a fresh auto-assigned
    key -- the synchronous counterpart to `mint_new_tickets`' own auto-key
    resolution, proven not to hand out the same key twice."""
    assert cli_main(["--home", str(home), "create", "First", "--json", "--no-nudge"]) == 0
    first = json.loads(capsys.readouterr().out)["key"]
    assert cli_main(["--home", str(home), "create", "Second", "--json", "--no-nudge"]) == 0
    second = json.loads(capsys.readouterr().out)["key"]
    assert first != second
    assert {first, second} == {"T-1", "T-2"}


def test_create_json_retry_on_existing_key_is_an_idempotent_no_op(home, capsys):
    """A crash-and-respawn retry of the same explicit-key `create --json` call
    (the reconciler's exactly-one-step contract) must not error or re-mint --
    it returns the already-minted key unchanged, matching `mint_new_tickets`'
    own crash-safety semantics for a key that already has events."""
    argv = ["--home", str(home), "create", "Retried ticket", "--key", "Q-2",
            "--json", "--no-nudge"]
    assert cli_main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli_main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second == {"key": "Q-2"}


def test_create_json_case_colliding_key_errors_instead_of_aliasing(home, capsys):
    """An explicit key that would alias an existing one only by letter case is
    rejected with a non-zero exit, same as the async mint path (RB-3) -- never
    silently aliased on a case-insensitive filesystem."""
    assert cli_main(["--home", str(home), "create", "Original", "--key", "Q-3",
                     "--json", "--no-nudge"]) == 0
    capsys.readouterr()
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = cli_main(["--home", str(home), "create", "Colliding", "--key", "q-3",
                       "--json", "--no-nudge"])
    assert rc != 0
    assert "case" in stderr.getvalue()
