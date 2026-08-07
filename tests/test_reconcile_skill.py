"""MR-6: /maestro-reconcile skill goes multi-repo. T-22: split by phase (progressive
disclosure) -- one command per phase instead of a single ~17KB file every reconciler
loaded in full regardless of what its phase needed.

The preamble resolves REPO/SLUG/BASE/PREFIX per-ticket via `maestro env --key` (falling
back to the legacy repo_path/branch_prefix fields for unbound tickets / single-repo
homes) instead of one global `maestro env` eval; every worktree/rebase `origin/main` and
every hardcoded `--repo cortop/maestro` gh call is gone. These tests cover: (1) a static
hardcode ban over every phase file's both copies, (2) mirror-sync between them per phase,
(3)/(4) the real env --key contract for two-repo and legacy single-repo shapes -- proven
by running the preamble's *actual* extracted eval code, not a re-typed copy, (5) the
Makefile `reconcile:` recipe, and (6) the dispatcher's per-phase command routing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from maestro import config as config_mod
from maestro.cli import main as cli_main
from maestro.dispatcher import dispatch, resolve_reconcile_command
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
# bound repo); "passive" is the one exception -- it never reads the repo, only HOME.
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
    """The bash code block under '## Always: load state first', where
    REPO/SLUG/BASE/PREFIX/HOME get resolved."""
    m = re.search(r"## Always: load state first\n.*?```bash\n(.*?)```", text, re.DOTALL)
    assert m, "could not find the preamble bash block"
    return m.group(1)


def _extract_transformer(preamble: str, needle: str) -> str:
    """Pull the python3 -c '...' script out of the one `eval "$(...)"` line in
    *preamble* containing *needle* -- the real code, not a re-typed copy."""
    for line in preamble.splitlines():
        if needle in line and "python3 -c" in line:
            m = re.search(r"python3 -c '(.*)'\)\"\s*$", line)
            assert m, f"could not parse the python3 transform out of: {line!r}"
            return m.group(1)
    raise AssertionError(f"no eval line containing {needle!r} found in preamble:\n{preamble}")


def _run_preamble_eval(script: str, json_text: str, var_names: list[str]) -> dict:
    """Actually `eval "$(cat <json> | python3 <script>)"` in a real bash subprocess
    (the same construct the skill uses) and echo back the resulting shell vars."""
    workdir = Path(tempfile.mkdtemp())
    try:
        json_file = workdir / "cli_output.json"
        json_file.write_text(json_text, encoding="utf-8")
        py_file = workdir / "transform.py"
        py_file.write_text(script, encoding="utf-8")
        echoes = "\n".join(f'echo "{v}=${{{v}}}"' for v in var_names)
        bash_script = workdir / "eval_test.sh"
        bash_script.write_text(
            f'eval "$(cat {json_file} | python3 {py_file})"\n{echoes}\n', encoding="utf-8")
        proc = subprocess.run(["bash", str(bash_script)], capture_output=True, text=True,
                              check=True)
        return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    for phase in REPO_AWARE_PHASE_FILES:
        for path in (_commands_path(phase), _skills_path(phase)):
            preamble = _preamble_block(_strip_frontmatter(path.read_text()))
            assert "maestro env --key" in preamble
            for var in ("REPO=", "SLUG=", "BASE=", "PREFIX=", "HOME="):
                assert var in preamble, f"{path}: preamble never assigns {var.rstrip('=')}"


def test_passive_phase_preamble_skips_unused_repo_vars():
    """No-op pruning (AC3): awaiting-ci/in-review/degraded/terminating never touch the
    bound repo, so their shared file's preamble resolves only HOME, not
    REPO/SLUG/BASE/PREFIX -- lines that would never change what the file does."""
    for path in (_commands_path("passive"), _skills_path("passive")):
        preamble = _preamble_block(_strip_frontmatter(path.read_text()))
        assert "HOME=" in preamble
        for var in ("REPO=", "SLUG=", "BASE=", "PREFIX="):
            assert var not in preamble, f"{path}: preamble resolves unused {var.rstrip('=')}"


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


def test_preamble_eval_resolves_bound_repo_for_two_repo_home(home, capsys):
    (home / "config.toml").write_text(MULTI_REPO_TOML, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cli_main(["--home", str(home), "create", "Multi-repo reconcile test",
                     "--key", "X-3", "--repo", "beta", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "X-3"]) == 0
    env_key_json = capsys.readouterr().out

    preamble = _preamble_block(_strip_frontmatter(_commands_path("triaging").read_text()))
    transform = _extract_transformer(preamble, "maestro env --key")
    result = _run_preamble_eval(transform, env_key_json, ["REPO", "SLUG", "BASE", "PREFIX"])

    assert result["REPO"] == "/repo/beta"
    assert result["SLUG"] == "acme/beta"
    assert result["BASE"] == "main"
    assert result["PREFIX"] == "maestro/"


# ---------------------------------------------------------------------------
# back-compat, legacy single-repo home, unbound ticket
# ---------------------------------------------------------------------------

LEGACY_SINGLE_REPO_TOML = """\
[maestro]
repo_path = "/repo/legacy"
branch_prefix = "legacy/"
"""


def test_preamble_eval_back_compat_single_repo_home(home, capsys):
    """No [repos.*] tables at all -- just the legacy single-repo config fields.
    An unbound ticket must still resolve REPO/PREFIX (from env --key, falling back
    to the legacy fields) and HOME (from the key-less env call) exactly as before."""
    (home / "config.toml").write_text(LEGACY_SINGLE_REPO_TOML, encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos == {}, "this home must have no [repos.*] tables (legacy shape)"

    assert cli_main(["--home", str(home), "create", "Legacy single-repo ticket",
                     "--key", "X-9", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "X-9"]) == 0
    env_key_json = capsys.readouterr().out

    capsys.readouterr()
    assert cli_main(["--home", str(home), "env"]) == 0
    env_json = capsys.readouterr().out

    preamble = _preamble_block(_strip_frontmatter(_commands_path("triaging").read_text()))
    key_transform = _extract_transformer(preamble, "maestro env --key")
    home_transform = _extract_transformer(preamble, "maestro env |")

    key_result = _run_preamble_eval(key_transform, env_key_json, ["REPO", "PREFIX"])
    home_result = _run_preamble_eval(home_transform, env_json, ["HOME"])

    assert key_result["REPO"] == cfg.repo_path
    assert key_result["PREFIX"] == cfg.branch_prefix
    assert home_result["HOME"] == str(home)


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
