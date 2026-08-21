"""MR-2: per-ticket repo binding data model — config [repos.*], repos.resolve()
precedence, `create --repo`, `env --key`. Zero consumer behavior change: nothing
yet reads a resolved binding to pick a worktree/cwd, so a full dispatch sweep
must still spawn identically to today (AC6).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import config as config_mod
from maestro import event_log, inbox, repos as repos_mod, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.dispatcher import dispatch
from maestro.sessions import DryRunSessions

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


def _write_multi_repo_config(home):
    (home / "config.toml").write_text(MULTI_REPO_TOML, encoding="utf-8")
    return config_mod.load(str(home))


# --- AC1: create --repo travels through mint -> spec frontmatter -> snapshot.repo ---

def test_create_repo_flows_through_mint_to_spec_and_snapshot(home):
    cfg = _write_multi_repo_config(home)
    rc = cli_main(["--home", str(home), "create", "Multi-repo ticket",
                   "--key", "X-1", "--repo", "beta", "--no-nudge"])
    assert rc == 0

    report = dispatch(cfg, DryRunSessions(), now=1000)
    assert "X-1" in report.minted

    events = event_log.read(home, "X-1")
    created = next(e for e in events if e["type"] == "TicketCreated")
    assert created["payload"]["repo"] == "beta"

    spec_text = store.spec_path(home, "X-1").read_text(encoding="utf-8")
    assert "repo: beta" in spec_text

    snap = snap_mod.load(home, "X-1")
    assert snap.repo == "beta"


# --- AC2: config.load parses [repos.*] tables; implicit default matches today's cfg ---

def test_config_load_parses_repos_tables(home):
    cfg = _write_multi_repo_config(home)
    assert cfg.repos["alpha"] == {
        "path": "/repo/alpha", "slug": "acme/alpha",
        "base_branch": "develop", "branch_prefix": "alpha/", "default": False,
        "max_spawns_per_sweep": None, "mode": "git", "reconcile_allowed_tools": [],
        "gh_account": None, "token_env": None, "prime": None, "base_drift_policy": None,
        "test_command": None,
        "prime_timeout": None, "worktree_timeout": None, "language": None,
        "test_selector": None,
    }
    assert cfg.repos["beta"] == {
        "path": "/repo/beta", "slug": "acme/beta",
        "base_branch": "main", "branch_prefix": "maestro/", "default": False,
        "max_spawns_per_sweep": None, "mode": "git", "reconcile_allowed_tools": [],
        "gh_account": None, "token_env": None, "prime": None, "base_drift_policy": None,
        "test_command": None,
        "prime_timeout": None, "worktree_timeout": None, "language": None,
        "test_selector": None,
    }


def test_no_repos_tables_yields_implicit_default_matching_repo_path(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/only"\nbranch_prefix = "solo/"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos == {}

    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.path == cfg.repo_path == "/repo/only"
    assert binding.branch_prefix == cfg.branch_prefix == "solo/"
    assert binding.prime is None  # no [maestro] prime set -- absent changes no existing behavior


# --- AC3: precedence proven by editing the real spec.md ---

def test_precedence_frontmatter_beats_payload_beats_default(home):
    cfg = _write_multi_repo_config(home)
    rc = cli_main(["--home", str(home), "create", "Precedence ticket",
                   "--key", "X-2", "--repo", "alpha", "--no-nudge"])
    assert rc == 0
    dispatch(cfg, DryRunSessions(), now=1000)

    # Payload says alpha, no frontmatter edit yet -> resolve() follows payload via snapshot.repo.
    binding = repos_mod.resolve(cfg, home, "X-2")
    assert binding.name == "alpha"

    # Edit the real spec.md frontmatter to beta -> frontmatter wins over the payload.
    spec_path = store.spec_path(home, "X-2")
    spec_text = spec_path.read_text(encoding="utf-8")
    spec_text = spec_text.replace("repo: alpha", "repo: beta")
    store.atomic_write(spec_path, spec_text)
    binding = repos_mod.resolve(cfg, home, "X-2")
    assert binding.name == "beta"

    # Delete the frontmatter line entirely -> falls back to the payload via snapshot.repo.
    spec_text = spec_path.read_text(encoding="utf-8")
    spec_text = "\n".join(l for l in spec_text.splitlines() if not l.startswith("repo:")) + "\n"
    store.atomic_write(spec_path, spec_text)
    binding = repos_mod.resolve(cfg, home, "X-2")
    assert binding.name == "alpha"  # payload's original repo, via snapshot.repo

    # A spec naming an unconfigured repo resolves to the implicit default, never raises.
    # Insert into the frontmatter (before the first "##" section) -- parse_spec_overrides
    # stops scanning at the first section header, matching parse_depends_on/etc.
    lines = spec_text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith("##"))
    lines.insert(header_idx, "repo: nope")
    store.atomic_write(spec_path, "\n".join(lines) + "\n")
    binding = repos_mod.resolve(cfg, home, "X-2")
    assert binding.name == "default"
    assert binding.path == cfg.repo_path


# --- AC4: `maestro env --key` ---

def test_env_key_prints_resolved_binding(home, capsys):
    cfg = _write_multi_repo_config(home)
    rc = cli_main(["--home", str(home), "create", "Env ticket",
                   "--key", "X-3", "--repo", "beta", "--no-nudge", "--json"])
    assert rc == 0
    # T-80: the mint-time seed spec has no acceptance criteria yet (a dangling
    # `- ` bullet) -- give it real ones, before the first sweep, so the new
    # missing-ACs due-gate lets this ticket reach `triaging` as normal, which
    # is what this test is actually about (`--json` mints synchronously, so
    # spec.md already exists here, ahead of any dispatch sweep).
    spec_path = store.spec_path(home, "X-3")
    store.atomic_write(spec_path, spec_path.read_text(encoding="utf-8") + "- [ ] ok\n")
    dispatch(cfg, DryRunSessions(), now=1000)

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "env", "--key", "X-3"])
    assert rc == 0
    out = capsys.readouterr().out
    import json
    printed = json.loads(out)
    assert printed == {"repo": "beta", "repo_path": "/repo/beta", "slug": "acme/beta",
                        "base_branch": "main", "branch_prefix": "maestro/", "mode": "git",
                        "gh_credential": None, "prime": None,
                        "reconcile_command": "/maestro-reconcile-triaging",
                        "disallowed_tools": ["Bash(gh pr merge:*)"],
                        # RF-2: per-key resolved spawn args, same as a real spawn would use.
                        "model": "sonnet", "effort": None,
                        "runner": "claude", "runner_model": None,
                        # MTO-6: served here so awaiting-human.md's KIND lookup needs no
                        # interpreter -- reused from GA-10's per-key env seam.
                        "kind": "implementation",
                        # T-96 AC4: the resolved test-verification knobs a `test:`
                        # annotation's presence gate actually uses.
                        "language": None, "test_command": None}


def test_env_key_unknown_repo_exits_nonzero(home, capsys):
    cfg = _write_multi_repo_config(home)
    store.atomic_write(store.spec_path(home, "X-4"),
                       "# X-4\napproval_tier: 1\nrepo: ghost\n\n## Intent\nx\n")
    event_log.append(home, "X-4", "TicketCreated",
                     {"title": "X-4", "source": "test", "spec_hash": "x"}, actor="d")
    snap_mod.rebuild(home, "X-4")

    rc = cli_main(["--home", str(home), "env", "--key", "X-4"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost" in err
    assert "Traceback" not in err


def test_bare_env_unchanged_key_set(home, capsys):
    cfg = _write_multi_repo_config(home)
    capsys.readouterr()
    rc = cli_main(["--home", str(home), "env"])
    assert rc == 0
    out = capsys.readouterr().out
    import json
    printed = json.loads(out)
    assert set(printed.keys()) == {"home", "board", "repo_path", "branch_prefix", "reconcile_command",
                                    "max_concurrency", "max_impl_turns", "qa_standards_axis",
                                    "providers", "spawn_floor_s", "no_output_timeout",
                                    "runner", "runner_model", "runner_enabled"}
    assert printed["no_output_timeout"] == cfg.no_output_timeout


# --- AC5: unknown --repo fails fast at create; old snapshots round-trip ---

def test_create_unknown_repo_fails_fast(home, capsys):
    cfg = _write_multi_repo_config(home)
    rc = cli_main(["--home", str(home), "create", "Bad repo ticket",
                   "--repo", "nope", "--no-nudge"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "alpha" in err and "beta" in err

    assert inbox.pending_new(home) == []
    report = dispatch(cfg, DryRunSessions(), now=1000)
    assert report.minted == []


def test_old_snapshot_without_repo_field_round_trips(home):
    d = {"key": "T-9", "phase": "ready", "observed_seq": 1}
    store.write_json(store.snapshot_path(home, "T-9"), d)
    snap = snap_mod.load(home, "T-9")
    assert snap.repo is None
    assert snap.key == "T-9"
    assert snap.phase == "ready"


# --- AC6 (superseded by MR-3, then QW-7): once dispatcher.py consumes repo
# bindings for cwd, a bound ticket's pre-worktree spawn is seeded from ITS
# repo, not cfg.repo_path -- but (QW-7) never lands IN that repo directly, a
# shared checkout the reconciler could edit. It lands in a per-key scratch
# dir whose .claude/commands symlink still resolves through the right repo. ---

def test_dispatch_spawn_cwd_honors_repo_binding(home):
    default_repo = home / "repo-default"
    alpha_repo = home / "repo-alpha"
    for repo in (default_repo, alpha_repo):
        (repo / ".claude" / "commands").mkdir(parents=True)
        (repo / ".claude" / "commands" / "maestro-reconcile-triaging.md").write_text("# x\n")
    (home / "config.toml").write_text(
        "[maestro]\n"
        f'repo_path = "{default_repo}"\n'
        'branch_prefix = "maestro/"\n\n'
        "[repos.alpha]\n"
        f'path = "{alpha_repo}"\n'
        'slug = "acme/alpha"\n'
        'base_branch = "develop"\n'
        'branch_prefix = "alpha/"\n',
        encoding="utf-8",
    )
    cfg = config_mod.load(str(home))
    assert cli_main(["--home", str(home), "create", "Ticket X-5",
                     "--key", "X-5", "--no-nudge", "--json"]) == 0
    assert cli_main(["--home", str(home), "create", "Ticket X-6",
                     "--key", "X-6", "--repo", "alpha", "--no-nudge", "--json"]) == 0
    # T-80: give both mint-time seed specs real acceptance criteria, before the
    # first sweep, so the new missing-ACs due-gate lets each ticket reach
    # `triaging` (and spawn) as normal, which is what this test is actually
    # about (`--json` mints synchronously, so spec.md already exists here,
    # ahead of any dispatch sweep).
    for key in ("X-5", "X-6"):
        spec_path = store.spec_path(home, key)
        store.atomic_write(spec_path, spec_path.read_text(encoding="utf-8") + "- [ ] ok\n")

    sessions = DryRunSessions()
    dispatch(cfg, sessions, now=1000)

    # No worktree exists yet for either key. Neither lands in its repo directly
    # (QW-7) -- each gets its own scratch dir, seeded from its OWN resolved
    # repo (X-5 unbound -> cfg.repo_path/default, X-6 bound -> alpha).
    cwd_by_key = {k: c for k, _p, c, _m, _e, _d, *_ in sessions.spawned}
    assert cwd_by_key["X-5"] == str(store.scratch_path(home, "X-5"))
    assert cwd_by_key["X-6"] == str(store.scratch_path(home, "X-6"))
    assert (Path(cwd_by_key["X-5"]) / ".claude" / "commands" / "maestro-reconcile-triaging.md").resolve() \
        == (default_repo / ".claude" / "commands" / "maestro-reconcile-triaging.md").resolve()
    assert (Path(cwd_by_key["X-6"]) / ".claude" / "commands" / "maestro-reconcile-triaging.md").resolve() \
        == (alpha_repo / ".claude" / "commands" / "maestro-reconcile-triaging.md").resolve()


# --- MR-4: resolve_vcs_slug / slug_from_pr_url -- the VCS-layer repo resolution ---

def test_slug_from_pr_url_parses_owner_repo():
    assert repos_mod.slug_from_pr_url("https://github.com/acme/beta/pull/9") == "acme/beta"


def test_slug_from_pr_url_none_for_unset_or_unparseable():
    assert repos_mod.slug_from_pr_url(None) is None
    assert repos_mod.slug_from_pr_url("not a url") is None
    assert repos_mod.slug_from_pr_url("https://gitlab.com/acme/beta/pull/9") is None


def test_resolve_vcs_slug_prefers_bound_repo_table_over_pr_url(home):
    cfg = _write_multi_repo_config(home)
    snap = snap_mod.Snapshot(key="X", repo="alpha",
                              pr_url="https://github.com/other/owner/pull/1")
    assert repos_mod.resolve_vcs_slug(cfg, snap) == "acme/alpha"


def test_resolve_vcs_slug_falls_back_to_pr_url_shim_when_unbound(home):
    cfg = _write_multi_repo_config(home)
    snap = snap_mod.Snapshot(key="X", pr_url="https://github.com/acme/beta/pull/9")
    assert repos_mod.resolve_vcs_slug(cfg, snap) == "acme/beta"


def test_resolve_vcs_slug_bound_to_unconfigured_name_falls_back_to_pr_url_shim(home):
    cfg = _write_multi_repo_config(home)
    snap = snap_mod.Snapshot(key="X", repo="ghost",
                              pr_url="https://github.com/acme/beta/pull/9")
    assert repos_mod.resolve_vcs_slug(cfg, snap) == "acme/beta"


def test_resolve_vcs_slug_none_when_neither_available(home):
    cfg = _write_multi_repo_config(home)
    snap = snap_mod.Snapshot(key="X")
    assert repos_mod.resolve_vcs_slug(cfg, snap) is None


# --- GA-20: [repos.<name>] prime / [maestro] prime fallback ---

def test_repos_table_prime_parses_and_resolves(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nprime = "npm ci"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["prime"] == "npm ci"
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.prime == "npm ci"


def test_maestro_prime_fallback_carried_by_implicit_default_dogfood_shape(home):
    """GA-20's recorded shape decision: a single-repo home with NO [repos.*]
    table at all -- the dogfood shape, `[maestro] repo_path = ...` only --
    declares its prime via a bare `[maestro] prime`, carried by
    repos.implicit_default."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/only"\nprime = "python3 -m venv .venv"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos == {}
    assert cfg.prime == "python3 -m venv .venv"

    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.name == "default"
    assert binding.prime == "python3 -m venv .venv"


def test_repos_default_table_prime_wins_over_maestro_prime_fallback(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nprime = "fallback"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\ndefault = true\nprime = "alpha prime"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    binding = repos_mod.implicit_default(cfg)
    assert binding.name == "alpha"
    assert binding.prime == "alpha prime"


# --- T-90: board-wide + per-repo prime_timeout/worktree_timeout ---

def test_prime_timeout_defaults_to_600(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.prime_timeout == 600


def test_prime_timeout_configurable_board_wide(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nprime_timeout = 900\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.prime_timeout == 900


def test_repos_table_timeouts_override_board_wide_default(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nprime_timeout = 900\nworktree_timeout = 1800\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nprime_timeout = 60\nworktree_timeout = 120\n\n'
        '[repos.beta]\npath = "/repo/beta"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["prime_timeout"] == 60
    assert cfg.repos["alpha"]["worktree_timeout"] == 120
    # unset in the table -- config.repos carries None; resolution (below) is what inherits
    assert cfg.repos["beta"]["prime_timeout"] is None
    assert cfg.repos["beta"]["worktree_timeout"] is None

    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    alpha_binding = repos_mod.resolve(cfg, home, "T-1")
    assert alpha_binding.prime_timeout == 60      # this table's own override wins
    assert alpha_binding.worktree_timeout == 120

    store.atomic_write(store.spec_path(home, "T-2"),
                       "# T-2\napproval_tier: 1\nrepo: beta\n\n## Intent\nx\n")
    beta_binding = repos_mod.resolve(cfg, home, "T-2")
    assert beta_binding.prime_timeout == 900      # unset -- inherits the board-wide default
    assert beta_binding.worktree_timeout == 1800


def test_default_config_template_documents_prime_timeout_and_worktree_timeout():
    from maestro.config import DEFAULT_CONFIG_TOML
    assert "prime_timeout" in DEFAULT_CONFIG_TOML
    assert "worktree_timeout" in DEFAULT_CONFIG_TOML
    # both keys are documented again inside the commented-out [repos.<name>] block,
    # beside `prime` -- not just the board-wide [maestro] block above it.
    repos_block = DEFAULT_CONFIG_TOML[DEFAULT_CONFIG_TOML.index("# [repos.<name>]"):]
    assert "prime_timeout" in repos_block
    assert "worktree_timeout" in repos_block


def test_repo_table_typo_prime_timeout_key_still_fails_closed(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nprim_timeout = 5\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError):
        config_mod.load(str(home))


# --- T-84: per-repo `language` -- selects the testlang table entry, fails
# closed at config.load() on an unrecognized value, unset means "python".

def test_repos_table_language_parses_and_resolves(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nlanguage = "go"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["language"] == "go"
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.language == "go"


def test_repos_table_language_unset_resolves_to_none_ie_python(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n[repos.alpha]\npath = "/repo/alpha"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["language"] is None
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.language is None


def test_repos_table_language_unsupported_fails_config_load_closed(home):
    """T-84 AC6: an unsupported/unconfigured language must fail legibly, never
    silently no-op into a `test:` check that fails closed forever. A typo'd
    value is caught here, at config.load(), before any ticket ever reaches
    `verifying` -- the most legible failure of all: the whole home refuses to
    load until the human fixes the typo."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nlanguage = "rust"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError, match="language must be one of"):
        config_mod.load(str(home))


# --- T-98: per-repo `test_selector` -- orthogonal to `language`, overrides
# ONLY the testlang profile's format_selector; fails closed at config.load()
# on an unknown placeholder/malformed template, unset stays byte-identical.

def test_repos_table_test_selector_parses_and_resolves(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nlanguage = "go"\n'
        "test_selector = \"bzl test //{dir}/... --test_filter='^({names})$'\"\n",
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["test_selector"] == "bzl test //{dir}/... --test_filter='^({names})$'"
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.test_selector == "bzl test //{dir}/... --test_filter='^({names})$'"


def test_repos_table_test_selector_unset_resolves_to_none(home):
    """AC1/AC5: unset `test_selector` resolves to `None` on the binding, so
    `testlang.selector_for` falls all the way back to the language profile's
    own `format_selector` -- every existing board stays byte-identical."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n[repos.alpha]\npath = "/repo/alpha"\n',
        encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["alpha"]["test_selector"] is None
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.test_selector is None


def test_repos_table_test_selector_unknown_placeholder_fails_config_load_closed(home):
    """AC2: a typo'd/unrecognized placeholder is caught here, at config.load(),
    naming the supported set -- exactly like `language`'s own fail-closed
    posture -- never a `test:` check that fails closed forever later."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\ntest_selector = "{test_command} {bogus}"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError, match="unrecognized placeholder"):
        config_mod.load(str(home))


def test_repos_table_test_selector_malformed_braces_fails_config_load_closed(home):
    """AC2: malformed braces (an unbalanced `{`) fail exactly like an unknown
    placeholder -- both are caught at load, never deferred to a `test:` check."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\ntest_selector = "{test_command} {names"\n',
        encoding="utf-8")
    with pytest.raises(store.MaestroError, match="test_selector"):
        config_mod.load(str(home))


# --- T-96 AC5: board-wide `[maestro] language` -- honored (inherited by
# repos.implicit_default and any [repos.<name>] table with no language of its
# own, same precedence as test_command), never silently ignored as it was
# before this ticket.

def test_board_wide_language_inherited_by_implicit_default(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nlanguage = "go"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.language == "go"
    binding = repos_mod.implicit_default(cfg)
    assert binding.language == "go"


def test_board_wide_language_inherited_by_a_repo_table_with_no_language_of_its_own(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nlanguage = "typescript"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.language == "typescript"


def test_a_repo_tables_own_language_wins_over_the_board_wide_default(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nlanguage = "go"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\nlanguage = "python"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\nrepo: alpha\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")
    assert binding.language == "python"


def test_board_wide_language_unsupported_fails_config_load_closed(home):
    """The other half of AC5 -- an unrecognized `[maestro] language` is
    rejected at load, never silently ignored the way it was before this
    field existed."""
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nlanguage = "cobol"\n', encoding="utf-8")
    with pytest.raises(store.MaestroError, match="language must be one of"):
        config_mod.load(str(home))
