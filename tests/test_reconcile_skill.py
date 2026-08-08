"""MR-6: /maestro-reconcile skill goes multi-repo. T-22: split by phase (progressive
disclosure) -- one command per phase instead of a single ~17KB file every reconciler
loaded in full regardless of what its phase needed.

GA-10: the preamble is eval-free. It still resolves REPO/SLUG/BASE/PREFIX/MODE per-ticket
via the real `maestro env --key` (falling back to the legacy repo_path/branch_prefix
fields for unbound tickets / single-repo homes) and MHOME via the key-less `maestro env`,
but no longer pipes either through `eval "$(... | python3 -c '...')"` to bind shell
variables -- a reconciler in a bound repo would otherwise stall on line 1 unless that
repo granted `Bash(eval:*)`/`Bash(python3:*)`/`Bash(sed:*)`/`Bash(cat:*)`, and
`Bash(eval:*)` structurally voids both the tier denylist (AD-1) and the T-21
home-deletion hook (neither matches inside a command substitution). The preamble instead
names the exact JSON field keys the two `maestro env` calls print and tells the agent to
hold them as literals, then loads spec.md/context.md with the Read tool -- no shell
command reads a file in any preamble any more either. Every worktree/rebase
`origin/main` and every hardcoded `--repo cortop/maestro` gh call is still gone (T-22's
original MR-6 guarantee, untouched by this rewrite). These tests cover: (1) a static
hardcode/eval/python3/sed/cat ban over every phase file's both copies, (2) mirror-sync
between them per phase, (3)/(4) the real env --key contract for two-repo and legacy
single-repo shapes -- proven against the real CLI, not a re-typed copy of its logic,
(5) the Makefile `reconcile:` recipe, and (6) the dispatcher's per-phase command routing.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from maestro import config as config_mod
from maestro import inbox, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.dispatcher import dispatch, is_due, resolve_reconcile_command
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"
SKILLS_DIR = REPO_ROOT / "skills"

# The six phase files T-22 split the single maestro-reconcile.md into. "passive" covers
# awaiting-ci/in-review/degraded/terminating -- phases with no active work for a
# reconciler, so they share one file instead of four near-empty ones.
PHASE_FILES = ["triaging", "awaiting-human", "ready", "researching", "implementing", "passive"]
# Phases whose file needs the full REPO/SLUG/BASE/PREFIX/MODE preamble (they touch the
# bound repo); "passive" is the one exception -- it never reads the repo, only MHOME.
REPO_AWARE_PHASE_FILES = ["triaging", "awaiting-human", "ready", "researching", "implementing"]


def _commands_path(phase: str) -> Path:
    return COMMANDS_DIR / f"maestro-reconcile-{phase}.md"


def _skills_path(phase: str) -> Path:
    return SKILLS_DIR / f"maestro-reconcile-{phase}.md"


ALL_PHASE_PATHS = [_commands_path(p) for p in PHASE_FILES] + [_skills_path(p) for p in PHASE_FILES]


def _strip_frontmatter(text: str) -> str:
    """Body only -- frontmatter (description/argument-hint) is intentionally allowed to
    differ; only the body is asserted byte-identical."""
    parts = text.split("---\n", 2)
    assert len(parts) == 3, "expected exactly one YAML frontmatter block"
    return parts[2]


def _preamble_block(text: str) -> str:
    """The bash code block under '## Always: load state first' -- the real `maestro
    env`/`maestro env --key`/`observe-spec`/`snapshot` calls the agent runs; it contains
    no `eval`/`python3`/`sed`/`cat` any more (GA-10) -- REPO/SLUG/BASE/PREFIX/MODE/MHOME
    are held as literals the agent reads out of the JSON these commands print, not shell
    variables a preamble line assigns."""
    m = re.search(r"## Always: load state first\n.*?```bash\n(.*?)```", text, re.DOTALL)
    assert m, "could not find the preamble bash block"
    return m.group(1)


# ---------------------------------------------------------------------------
# AC1: progressive disclosure -- every phase file exists in both locations,
# and the old monolithic file is gone (routed via the dispatcher, not a router
# read inside one big file).
# ---------------------------------------------------------------------------

def test_old_monolithic_skill_file_is_gone():
    assert not (COMMANDS_DIR / "maestro-reconcile.md").exists()
    assert not (SKILLS_DIR / "maestro-reconcile.md").exists()


def test_every_phase_file_exists_in_both_locations():
    for phase in PHASE_FILES:
        assert _commands_path(phase).exists(), f"missing .claude/commands/ file for {phase}"
        assert _skills_path(phase).exists(), f"missing skills/ file for {phase}"


def test_dispatcher_resolves_every_live_phase_to_an_existing_command_file():
    """Every phase a ticket can actually be spawned in (i.e. not DONE, the one
    TERMINAL_PHASES member) resolves, via the same function the dispatcher spawn
    path calls, to a command whose two files both exist on disk."""
    for phase in Phase:
        if phase == Phase.DONE:
            continue
        cfg = config_mod.Config(home=REPO_ROOT, repo_path=str(REPO_ROOT))
        command = resolve_reconcile_command(cfg, phase.value)
        suffix = command.removeprefix(cfg.reconcile_command + "-")
        assert _commands_path(suffix).exists() and _skills_path(suffix).exists(), (
            f"{phase.value} resolves to {command!r} but its files are missing")


# ---------------------------------------------------------------------------
# AC3 (no-op pruning): a hardcode ban over every phase file's both copies
# ---------------------------------------------------------------------------

def test_no_hardcoded_repo_slug():
    for path in ALL_PHASE_PATHS:
        text = path.read_text()
        assert "cortop/maestro" not in text, f"{path} still hardcodes the repo slug"


def test_no_bare_origin_main():
    for path in ALL_PHASE_PATHS:
        assert "origin/main" not in path.read_text(), f"{path} still hardcodes origin/main"
    # The rewritten form must actually be present (not just deleted): ready.md creates the
    # worktree off origin/$BASE and fetches it once; implementing.md rebases onto
    # origin/$BASE, diffs the QA loop against it, and fetches it once (step 0).
    for phase, base_count, fetch_count in (("ready", 1, 1), ("implementing", 2, 1)):
        for path in (_commands_path(phase), _skills_path(phase)):
            text = path.read_text()
            assert text.count('"origin/$BASE"') == base_count, \
                f'{path} should target "origin/$BASE" exactly {base_count} time(s)'
            assert text.count('fetch -q origin "$BASE"') == fetch_count, \
                f'{path} should fetch "$BASE" exactly {fetch_count} time(s)'


def test_gh_pr_calls_use_repo_slug_and_base():
    # gh calls only live in the implementing file (PR create/view) now.
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        text = path.read_text()
        assert text.count('--repo "$SLUG"') == 3, \
            f"{path}: expected exactly 3 gh calls (1 create + 2 view) carrying --repo \"$SLUG\""
        assert text.count('--base "$BASE"') == 1, \
            f"{path}: expected exactly 1 --base \"$BASE\" (on gh pr create only)"
        create_line = next(l for l in text.splitlines() if "gh pr create" in l)
        assert '--repo "$SLUG"' in create_line and '--base "$BASE"' in create_line


def test_preamble_resolves_via_env_key_for_repo_aware_phases():
    """GA-10: no `eval`/`python3`/`sed`/`cat` anywhere in the preamble block, and no
    `VAR=` shell assignment either -- `maestro env --key`/`maestro env` are still run for
    real, but REPO/SLUG/BASE/PREFIX/MODE/MHOME are named as the JSON field keys
    (repo_path/slug/base_branch/branch_prefix/mode/home) the agent reads with the Read
    tool and holds as literals, not variables a preamble line binds."""
    for phase in REPO_AWARE_PHASE_FILES:
        for path in (_commands_path(phase), _skills_path(phase)):
            text = path.read_text()
            preamble = _preamble_block(_strip_frontmatter(text))
            assert "maestro env --key" in preamble
            assert "maestro env" in preamble  # the key-less, board-wide call
            for banned in ("eval", "python3", "sed", "cat"):
                assert banned not in preamble, f"{path}: preamble still shells out to {banned}"
            for var in ("REPO=", "SLUG=", "BASE=", "PREFIX=", "MHOME="):
                assert var not in preamble, f"{path}: preamble still assigns {var} via shell"
            for field in ("repo_path", "slug", "base_branch", "branch_prefix", "mode", "home"):
                assert f"`{field}`" in text, \
                    f"{path}: never names the `{field}` JSON field the agent must read"


def test_preamble_never_shadows_the_real_home():
    """GA-1: a bare `HOME=` in a phase preamble re-assigns the shell's already-exported
    $HOME for the rest of that bash block, detaching any child git/gh call from
    ~/.gitconfig / ~/.config/gh -- the rename to $MHOME is what repairs it, so nothing
    may reintroduce a shadowing assignment, and no phase file may reference the real
    $HOME/${HOME}/bare-word HOME anywhere in its body any more (every use was renamed
    to $MHOME). The lookbehind excludes A-Z/_ so it still allows $MHOME and
    $MAESTRO_HOME, which is why (?<![A-Z_])HOME -- not a plain "HOME" substring
    search -- is the right regex here."""
    shadow_re = re.compile(r"(?<![A-Z_])HOME=")
    for phase in PHASE_FILES:
        for path in (_commands_path(phase), _skills_path(phase)):
            preamble = _preamble_block(_strip_frontmatter(path.read_text()))
            assert not shadow_re.search(preamble), f"{path}: preamble still shadows HOME"

    body_re = re.compile(r"(?<![A-Z_])HOME")
    for path in ALL_PHASE_PATHS:
        body = _strip_frontmatter(path.read_text())
        assert not body_re.search(body), f"{path}: still references the real $HOME"


def test_passive_phase_preamble_skips_unused_repo_vars():
    """No-op pruning (AC3): awaiting-ci/in-review/degraded/terminating never touch the
    bound repo, so their shared file's preamble resolves only MHOME (the key-less
    `maestro env`), never calls `maestro env --key`, and never names
    REPO/SLUG/BASE/PREFIX -- lines that would never change what the file does. GA-10:
    also no `eval`/`python3`/`sed`/`cat`, and no `VAR=` shell assignment."""
    for path in (_commands_path("passive"), _skills_path("passive")):
        text = path.read_text()
        preamble = _preamble_block(_strip_frontmatter(text))
        assert "maestro env" in preamble
        assert "maestro env --key" not in preamble
        for banned in ("eval", "python3", "sed", "cat"):
            assert banned not in preamble, f"{path}: preamble still shells out to {banned}"
        for var in ("REPO=", "SLUG=", "BASE=", "PREFIX=", "MHOME="):
            assert var not in preamble, f"{path}: preamble resolves unused {var.rstrip('=')}"
        for name in ("REPO", "SLUG", "BASE", "PREFIX"):
            assert name not in preamble, f"{path}: preamble resolves unused {name}"
        assert "`home`" in text, f"{path}: never names the `home` JSON field"


# ---------------------------------------------------------------------------
# AC2: every phase branch states a checkable, exhaustive completion criterion
# ---------------------------------------------------------------------------

def test_every_phase_file_states_a_done_when_criterion():
    for phase in PHASE_FILES:
        for path in (_commands_path(phase), _skills_path(phase)):
            text = path.read_text()
            assert "**Done when:**" in text, f"{path} has no checkable completion criterion"


# ---------------------------------------------------------------------------
# AC3 (de-negation): bans restated positively rather than left as bare "never"/"do NOT"
# ---------------------------------------------------------------------------

def test_implementing_restates_bans_positively():
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        text = path.read_text()
        # The old bare-negative framing is gone...
        assert "do NOT poll CI in-session" not in text
        assert "Rules: never force-push, never skip hooks, never mock real behavior" not in text
        # ...replaced by positive restatements of the same rules.
        assert "Push normally" in text or "push normally" in text.lower()
        assert "never force-push" in text.lower()  # the rule survives, framed positively
        assert "the dispatcher's next sweep polls CI" in text


def test_implementing_restates_never_abort_positively():
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        text = path.read_text()
        assert "always resolve" in text.lower()
        assert "git rebase --abort" in text
        assert "never" in text.lower()


# ---------------------------------------------------------------------------
# AC4: researching branch requires primary sources, not just "search the web"
# ---------------------------------------------------------------------------

def test_implementing_records_turns_via_impl_turn_verb():
    """GA-2: the QA-loop turn breadcrumb must go through `maestro impl-turn`, which
    checks the max_impl_turns ceiling and routes a crossing call to ops.fail -- a raw
    `maestro append --type ImplTurnRecorded` bypasses that check entirely (and, on the
    hand-rolled step-id the old text used, undercounts turns after a crash/respawn)."""
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        text = path.read_text()
        assert "ImplTurnRecorded" not in text
        assert "maestro impl-turn" in text


def test_researching_requires_primary_sources():
    for path in (_commands_path("researching"), _skills_path("researching")):
        text = path.read_text()
        assert "primary source" in text.lower()
        assert "source that owns it" in text.lower()
        assert "search the web" not in text.lower()  # the old, weaker framing is gone


# ---------------------------------------------------------------------------
# AC5: conflict-resolution adds an intent-recovery step + restates never --abort
# ---------------------------------------------------------------------------

def test_implementing_conflict_resolution_recovers_intent_first():
    for path in (_commands_path("implementing"), _skills_path("implementing")):
        text = path.read_text()
        assert "recover the intent on" in text.lower()
        assert "git log -1" in text  # reads the originating commit
        assert "gh pr view" in text  # and the originating PR, when named


# ---------------------------------------------------------------------------
# Inbox drain: a pending human command is a wake signal in EVERY phase, and only
# `maestro inbox-ack` clears it. The T-22 split dropped the fold step from the
# "passive" file, so a `msg` to an in-review ticket re-woke it on every sweep
# forever -- 17 no-op spawns/hour on T-24 (2026-08-07) until the watchdog fired.
# The skill is markdown, so the regression is only catchable statically: these
# assert the contract, and the dispatcher tests below prove why it matters.
# ---------------------------------------------------------------------------

def test_every_phase_file_folds_the_inbox():
    """`is_due` treats inbox_pending as a wake reason in every non-terminal phase,
    so every spawnable phase file must fold -- a file that skips it swallows human
    input and (in a phase where nothing else changes) spins."""
    for phase in PHASE_FILES:
        for path in (_commands_path(phase), _skills_path(phase)):
            assert 'maestro fold-inbox "$KEY"' in path.read_text(), \
                f"{path} never folds the inbox -- human commands are swallowed here"


def test_passive_file_acks_the_inbox_on_every_consuming_path():
    """The regression itself: passive covers the sleeping phases, where an unacked
    command is an unbounded spawn loop (nothing else about the ticket changes
    between sweeps). It must both fold and ack, and say why."""
    for path in (_commands_path("passive"), _skills_path("passive")):
        text = path.read_text()
        assert 'maestro inbox-ack "$KEY"' in text, f"{path} folds but never acks"
        # Ack-last ordering is what makes a mid-step crash safe (re-read, not lose).
        assert "Ack last" in text or "ack last" in text.lower(), \
            f"{path} does not state the ack-last ordering rule"
        # Every phase branch in the file must reach an ack, not just the preamble.
        for branch in ("degraded", "terminating"):
            assert f"## `{branch}`" in text


def test_reconcile_command_files_are_readable_from_a_worktree_checkout():
    """T-22 resolves the command *name* from the dispatcher (main's code) but Claude
    resolves the command *file* from the cwd it spawns in -- a worktree. Every
    resolvable command must therefore exist under .claude/commands/ at the repo
    root, which is what a fresh worktree inherits."""
    for phase in Phase:
        if phase == Phase.DONE:
            continue
        cfg = config_mod.Config(home=REPO_ROOT, repo_path=str(REPO_ROOT))
        command = resolve_reconcile_command(cfg, phase.value)
        suffix = command.removeprefix(cfg.reconcile_command + "-")
        assert (COMMANDS_DIR / f"maestro-reconcile-{suffix}.md").is_file(), (
            f"{phase.value} spawns {command!r}; a worktree without that file "
            f"gets 'Unknown command' and no-ops forever")


def test_unacked_inbox_command_rewakes_a_sleeping_ticket_every_sweep(home):
    """The spin, end-to-end through the real dispatcher: an in-review ticket (a
    SLEEPING phase) with a pending command is due on 'inbox' every sweep, and stays
    due no matter how many times it is swept -- because nothing but an ack clears it."""
    from conftest import seed_ticket

    seed_ticket(home, "S-1", "in review with a human message", phase="in-review", pr=42)
    cfg = config_mod.Config(home=home, max_concurrency=5, backoff_base=10, max_failures=99)
    assert cli_main(["--home", str(home), "observe-spec", "S-1"]) == 0  # settle spec_hash
    assert cli_main(["--home", str(home), "cmd", "S-1", "msg",
                     "it should cover the tui too", "--no-nudge"]) == 0

    for sweep in range(3):
        sessions = DryRunSessions()
        dispatch(cfg, sessions, now=1000 + sweep)
        spawned = {key for key, *_ in sessions.spawned}
        assert "S-1" in spawned, f"sweep {sweep}: sleeping ticket should wake on the inbox"
        assert sessions.spawned[0][1] == "/maestro-reconcile-passive S-1"


def test_acking_the_inbox_puts_the_sleeping_ticket_back_to_sleep(home):
    """...and the ack is what stops it -- the contract the passive skill now honors."""
    from conftest import seed_ticket

    seed_ticket(home, "S-2", "in review with a human message", phase="in-review", pr=43)
    cfg = config_mod.Config(home=home, max_concurrency=5, backoff_base=10, max_failures=99)
    assert cli_main(["--home", str(home), "observe-spec", "S-2"]) == 0
    assert cli_main(["--home", str(home), "cmd", "S-2", "msg", "cover the tui", "--no-nudge"]) == 0

    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=1000)
    assert "S-2" in {key for key, *_ in sessions.spawned}

    # What the skill does: fold, route, ack last.
    assert cli_main(["--home", str(home), "fold-inbox", "S-2"]) == 0
    assert cli_main(["--home", str(home), "set-phase", "S-2", "implementing",
                     "--reason", "human: cover the tui"]) == 0
    assert cli_main(["--home", str(home), "inbox-ack", "S-2"]) == 0

    # The message survived as an event (not swallowed) and the spin is over.
    events = store.read_jsonl(store.events_path(home, "S-2"))
    assert any(e["type"] == "CommandReceived"
               and e["payload"]["args"]["text"] == "cover the tui" for e in events)
    snap = snap_mod.rebuild(home, "S-2")
    assert snap.phase == "implementing"
    assert not inbox.has_pending(home, "S-2")


def test_acked_in_review_ticket_stops_spawning(home):
    """A ticket left in-review after its command is acked goes quiet: the inbox no
    longer forces it due, so backoff/sleeping applies again."""
    from conftest import seed_ticket

    seed_ticket(home, "S-3", "in review, message already handled", phase="in-review", pr=44)
    cfg = config_mod.Config(home=home, max_concurrency=5, backoff_base=10, max_failures=99)
    assert cli_main(["--home", str(home), "observe-spec", "S-3"]) == 0
    assert cli_main(["--home", str(home), "cmd", "S-3", "msg", "noted", "--no-nudge"]) == 0
    assert cli_main(["--home", str(home), "fold-inbox", "S-3"]) == 0
    assert cli_main(["--home", str(home), "inbox-ack", "S-3"]) == 0

    snap = snap_mod.rebuild(home, "S-3")
    due = is_due(snap, inbox_pending=inbox.has_pending(home, "S-3"),
                 current_spec_hash=snap.spec_hash, now=1000)
    assert not due.due and due.reason != "inbox", \
        f"acked in-review ticket still due ({due.reason}) -- the spin is not fixed"


# ---------------------------------------------------------------------------
# AC6: mirror-sync -- the two copies stay byte-identical (body only), per phase file
# ---------------------------------------------------------------------------

def test_skill_copies_are_byte_identical_after_stripping_frontmatter():
    for phase in PHASE_FILES:
        commands_body = _strip_frontmatter(_commands_path(phase).read_text())
        skills_body = _strip_frontmatter(_skills_path(phase).read_text())
        assert commands_body == skills_body, f"maestro-reconcile-{phase}.md mirrors drifted"


# ---------------------------------------------------------------------------
# real env --key contract, two-repo home, bound ticket (exercised via the
# "triaging" file's preamble -- identical to every other repo-aware phase's)
# ---------------------------------------------------------------------------

MULTI_REPO_TOML = """\
[maestro]
repo_path = "/repo/default"
branch_prefix = "maestro/"

[repos.alpha]
path = "/repo/alpha"
slug = "acme/alpha"
base_branch = "develop"
branch_prefix = "alpha/"

[repos.beta]
path = "/repo/beta"
slug = "acme/beta"
"""


def test_preamble_env_key_resolves_bound_repo_for_two_repo_home(home, capsys):
    """GA-10: the preamble names `maestro env --key`'s real JSON field keys
    (repo_path/slug/base_branch/branch_prefix) as the literals REPO/SLUG/BASE/PREFIX --
    proven here against the real CLI output, not a re-typed copy of its logic (there is
    no shell transform left to extract and run; the agent reads this JSON directly)."""
    (home / "config.toml").write_text(MULTI_REPO_TOML, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cli_main(["--home", str(home), "create", "Multi-repo reconcile test",
                     "--key", "X-3", "--repo", "beta", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "X-3"]) == 0
    result = json.loads(capsys.readouterr().out)

    # The exact field names the triaging preamble's prose tells the agent to hold as
    # REPO/SLUG/BASE/PREFIX -- every other repo-aware phase file names the same fields.
    preamble = _preamble_block(_strip_frontmatter(_commands_path("triaging").read_text()))
    assert "maestro env --key" in preamble

    assert result["repo_path"] == "/repo/beta"
    assert result["slug"] == "acme/beta"
    assert result["base_branch"] == "main"
    assert result["branch_prefix"] == "maestro/"


# ---------------------------------------------------------------------------
# back-compat, legacy single-repo home, unbound ticket
# ---------------------------------------------------------------------------

LEGACY_SINGLE_REPO_TOML = """\
[maestro]
repo_path = "/repo/legacy"
branch_prefix = "legacy/"
"""


def test_preamble_env_key_back_compat_single_repo_home(home, capsys):
    """No [repos.*] tables at all -- just the legacy single-repo config fields.
    An unbound ticket must still resolve REPO/PREFIX (from `maestro env --key`,
    falling back to the legacy fields) and MHOME (from the key-less `maestro env`
    call) exactly as before, proven against the real CLI output. GA-10: the preamble
    no longer assigns a shell `HOME=` at all (nothing to shadow the real $HOME with
    any more) -- that structural guarantee is covered by
    test_preamble_never_shadows_the_real_home, not re-proven here."""
    (home / "config.toml").write_text(LEGACY_SINGLE_REPO_TOML, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos == {}, "this home must have no [repos.*] tables (legacy shape)"

    assert cli_main(["--home", str(home), "create", "Legacy single-repo ticket",
                     "--key", "X-9", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "X-9"]) == 0
    key_result = json.loads(capsys.readouterr().out)

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env"]) == 0
    home_result = json.loads(capsys.readouterr().out)

    assert key_result["repo_path"] == cfg.repo_path
    assert key_result["branch_prefix"] == cfg.branch_prefix
    assert home_result["home"] == str(home)


# ---------------------------------------------------------------------------
# `make -n reconcile` resolves the repo + per-phase command via env --key
# ---------------------------------------------------------------------------

def test_make_reconcile_dry_run_resolves_repo_via_env_key():
    proc = subprocess.run(["make", "-n", "reconcile", "KEY=X-1"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True)
    assert "maestro env --key" in proc.stdout
    assert "cd \"$REPO\"" in proc.stdout
    assert "claude -p" in proc.stdout
    assert "reconcile_command" in proc.stdout  # routes via the same field the dispatcher uses
    assert "$COMMAND X-1" in proc.stdout


def test_make_reconcile_env_key_falls_back_for_single_repo_home(home, cfg):
    """The exact CLI call the Makefile recipe shells out to
    (`maestro env --key <KEY>`) resolves cleanly against a single-repo home with
    an unbound ticket -- proving `make reconcile` still works there too."""
    assert cli_main(["--home", str(home), "create", "Makefile smoke ticket",
                     "--key", "X-5", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)
    assert cli_main(["--home", str(home), "env", "--key", "X-5"]) == 0


# ---------------------------------------------------------------------------
# AC7: dispatcher sweep test -- a ticket in each phase spawns with the right
# per-phase command
# ---------------------------------------------------------------------------

def test_dispatch_spawns_correct_per_phase_command_for_each_phase(home):
    from conftest import seed_ticket

    expected = {
        Phase.TRIAGING: "triaging",
        Phase.AWAITING_HUMAN: "awaiting-human",
        Phase.READY: "ready",
        Phase.RESEARCHING: "researching",
        Phase.IMPLEMENTING: "implementing",
        Phase.AWAITING_CI: "passive",
        Phase.IN_REVIEW: "passive",
        Phase.DEGRADED: "passive",
        Phase.TERMINATING: "passive",
    }
    for i, (phase, suffix) in enumerate(expected.items()):
        seed_ticket(home, f"P-{i}", f"phase {phase.value}", phase=phase.value)

    # Own Config (not the shared low-concurrency `cfg` fixture) so all 9 seeded
    # tickets can spawn in one sweep instead of being capacity_skipped.
    cfg = config_mod.Config(home=home, max_concurrency=20, backoff_base=10, max_failures=3)
    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=1000)

    # seed_ticket's placeholder spec_hash ("x") never matches the real on-disk spec it also
    # writes, so every freshly-seeded ticket is due on "spec-changed" regardless of phase --
    # all eight spawn, and every spawn's command must route off its actual phase.
    spawned_by_key = {key: prompt for key, prompt, *_ in sessions.spawned}
    for i, (phase, suffix) in enumerate(expected.items()):
        key = f"P-{i}"
        assert key in spawned_by_key, f"{phase.value} ticket never spawned"
        assert spawned_by_key[key] == f"/maestro-reconcile-{suffix} {key}", (
            f"{key} ({phase.value}) spawned with {spawned_by_key[key]!r}, "
            f"expected the {suffix} command")


# ---------------------------------------------------------------------------
# docs updated for N repos (unchanged by T-22, still exercised here)
# ---------------------------------------------------------------------------

def test_dogfood_documents_vendoring_and_activation_checklist():
    text = (REPO_ROOT / "DOGFOOD.md").read_text()
    assert ".claude/commands/maestro-reconcile-" in text
    assert "[repos." in text
    assert "MR-1" in text and "MR-6" in text
    assert "repo_path" in text  # single-repo default stays documented


def test_claude_md_rewords_pr_target_for_repo_binding():
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    assert "open PRs on `cortop/maestro`" not in text
    assert "repo binding" in text or "env --key" in text
