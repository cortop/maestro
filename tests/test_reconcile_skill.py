"""MR-6: /maestro-reconcile skill goes multi-repo.

The preamble now resolves REPO/SLUG/BASE/PREFIX per-ticket via `maestro env --key`
(falling back to the legacy repo_path/branch_prefix fields for unbound tickets /
single-repo homes) instead of one global `maestro env` eval; every worktree/rebase
`origin/main` and every hardcoded `--repo cortop/maestro` gh call is gone. These
tests cover: (1) a static hardcode ban over both skill copies, (2) mirror-sync
between them, (3)/(4) the real env --key contract for two-repo and legacy
single-repo shapes -- proven by running the preamble's *actual* extracted eval
code, not a re-typed copy, and (5) the Makefile `reconcile:` recipe.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from maestro import config as config_mod
from maestro.cli import main as cli_main
from maestro.dispatcher import dispatch
from maestro.sessions import DryRunSessions

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_SKILL = REPO_ROOT / ".claude" / "commands" / "maestro-reconcile.md"
SKILLS_SKILL = REPO_ROOT / "skills" / "maestro-reconcile.md"
BOTH_SKILLS = [COMMANDS_SKILL, SKILLS_SKILL]


def _strip_frontmatter(text: str) -> str:
    """Body only -- frontmatter (description/allowed-tools/argument-hint) is
    intentionally allowed to differ (commands/ carries an extra allowed-tools
    line); only the body is asserted byte-identical."""
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
# AC1: static hardcode ban -- fails on any future regression
# ---------------------------------------------------------------------------

def test_no_hardcoded_repo_slug():
    for path in BOTH_SKILLS:
        text = path.read_text()
        assert "cortop/maestro" not in text, f"{path} still hardcodes the repo slug"


def test_no_bare_origin_main():
    for path in BOTH_SKILLS:
        text = path.read_text()
        assert "origin/main" not in text, f"{path} still hardcodes origin/main"
        # the rewritten form must actually be present (not just deleted): worktree-add,
        # rebase, and the QA-loop diff all target "origin/$BASE"; both fetches pass "$BASE".
        assert text.count('"origin/$BASE"') == 3, \
            f'{path} should target "origin/$BASE" exactly three times (worktree-add + rebase + QA diff)'
        assert text.count('fetch -q origin "$BASE"') == 2, \
            f'{path} should fetch "$BASE" exactly twice (ready + implementing step 0)'


def test_gh_pr_calls_use_repo_slug_and_base():
    for path in BOTH_SKILLS:
        text = path.read_text()
        assert text.count('--repo "$SLUG"') == 3, \
            f"{path}: expected exactly 3 gh calls (1 create + 2 view) carrying --repo \"$SLUG\""
        assert text.count('--base "$BASE"') == 1, \
            f"{path}: expected exactly 1 --base \"$BASE\" (on gh pr create only)"
        create_line = next(l for l in text.splitlines() if "gh pr create" in l)
        assert '--repo "$SLUG"' in create_line and '--base "$BASE"' in create_line


def test_preamble_resolves_via_env_key():
    for path in BOTH_SKILLS:
        preamble = _preamble_block(_strip_frontmatter(path.read_text()))
        assert "maestro env --key" in preamble
        for var in ("REPO=", "SLUG=", "BASE=", "PREFIX=", "HOME="):
            assert var in preamble, f"{path}: preamble never assigns {var.rstrip('=')}"


# ---------------------------------------------------------------------------
# AC2: mirror-sync -- the two copies stay byte-identical (body only)
# ---------------------------------------------------------------------------

def test_skill_copies_are_byte_identical_after_stripping_frontmatter():
    commands_body = _strip_frontmatter(COMMANDS_SKILL.read_text())
    skills_body = _strip_frontmatter(SKILLS_SKILL.read_text())
    assert commands_body == skills_body


# ---------------------------------------------------------------------------
# AC3: real env --key contract, two-repo home, bound ticket
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

    preamble = _preamble_block(_strip_frontmatter(COMMANDS_SKILL.read_text()))
    transform = _extract_transformer(preamble, "maestro env --key")
    result = _run_preamble_eval(transform, env_key_json, ["REPO", "SLUG", "BASE", "PREFIX"])

    assert result["REPO"] == "/repo/beta"
    assert result["SLUG"] == "acme/beta"
    assert result["BASE"] == "main"
    assert result["PREFIX"] == "maestro/"


# ---------------------------------------------------------------------------
# AC4: back-compat, legacy single-repo home, unbound ticket
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

    preamble = _preamble_block(_strip_frontmatter(COMMANDS_SKILL.read_text()))
    key_transform = _extract_transformer(preamble, "maestro env --key")
    home_transform = _extract_transformer(preamble, "maestro env |")

    key_result = _run_preamble_eval(key_transform, env_key_json, ["REPO", "PREFIX"])
    home_result = _run_preamble_eval(home_transform, env_json, ["HOME"])

    assert key_result["REPO"] == cfg.repo_path
    assert key_result["PREFIX"] == cfg.branch_prefix
    assert home_result["HOME"] == str(home)


# ---------------------------------------------------------------------------
# AC5: `make -n reconcile` resolves the repo via env --key
# ---------------------------------------------------------------------------

def test_make_reconcile_dry_run_resolves_repo_via_env_key():
    proc = subprocess.run(["make", "-n", "reconcile", "KEY=X-1"], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True)
    assert "maestro env --key" in proc.stdout
    assert "cd \"$REPO\"" in proc.stdout
    assert "claude -p" in proc.stdout
    assert "/maestro-reconcile X-1" in proc.stdout


def test_make_reconcile_env_key_falls_back_for_single_repo_home(home, cfg):
    """The exact CLI call the Makefile recipe shells out to
    (`maestro env --key <KEY>`) resolves cleanly against a single-repo home with
    an unbound ticket -- proving `make reconcile` still works there too."""
    assert cli_main(["--home", str(home), "create", "Makefile smoke ticket",
                     "--key", "X-5", "--no-nudge"]) == 0
    dispatch(cfg, DryRunSessions(), now=1000)
    assert cli_main(["--home", str(home), "env", "--key", "X-5"]) == 0


# ---------------------------------------------------------------------------
# AC6: docs updated for N repos
# ---------------------------------------------------------------------------

def test_dogfood_documents_vendoring_and_activation_checklist():
    text = (REPO_ROOT / "DOGFOOD.md").read_text()
    assert ".claude/commands/maestro-reconcile.md" in text
    assert "[repos." in text
    assert "MR-1" in text and "MR-6" in text
    assert "repo_path" in text  # single-repo default stays documented


def test_claude_md_rewords_pr_target_for_repo_binding():
    text = (REPO_ROOT / "CLAUDE.md").read_text()
    assert "open PRs on `cortop/maestro`" not in text
    assert "repo binding" in text or "env --key" in text
