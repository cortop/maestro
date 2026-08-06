"""AD-6: non-git implementer path for vault/self-editing-skill targets.

A `[repos.<name>]` binding can declare `mode = "local"` — a plain directory with
no branch/PR path. Its ticket reaches `done` by writing in place, with
backup-on-write (`maestro local-backup`) as the compensating control for
skipping the PR review checkpoint a `mode = "git"` binding gets for free.
"""
from __future__ import annotations

import tarfile

from maestro import config as config_mod
from maestro import event_log, ops, repos as repos_mod, snapshot as snap_mod, store
from maestro.cli import main as cli_main
from maestro.statemachine import Phase


def _write_local_config(home, target):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\nbranch_prefix = "maestro/"\n\n'
        f'[repos.vault]\npath = "{target}"\nmode = "local"\n',
        encoding="utf-8",
    )
    return config_mod.load(str(home))


# --- AC1: a binding can declare mode = "local" and repos.resolve() carries it ---

def test_resolve_carries_local_mode(home, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    cfg = _write_local_config(home, target)
    store.atomic_write(store.spec_path(home, "V-1"),
                       "# V-1\napproval_tier: 0\nrepo: vault\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "V-1")
    assert binding.mode == "local"
    assert binding.path == str(target)


def test_default_binding_is_git_mode(home, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    cfg = _write_local_config(home, target)
    store.atomic_write(store.spec_path(home, "T-1"),
                       "# T-1\napproval_tier: 1\n\n## Intent\nx\n")
    binding = repos_mod.resolve(cfg, home, "T-1")  # unbound -> implicit default
    assert binding.mode == "git"


def test_unrecognized_mode_falls_back_to_git(home, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    (home / "config.toml").write_text(
        f'[repos.vault]\npath = "{target}"\nmode = "bogus"\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.repos["vault"]["mode"] == "git"
    binding = repos_mod._binding_from_table("vault", cfg.repos["vault"])
    assert binding.mode == "git"


def test_env_key_prints_local_mode(home, tmp_path, capsys):
    target = tmp_path / "vault"
    target.mkdir()
    _write_local_config(home, target)
    store.atomic_write(store.spec_path(home, "V-1"),
                       "# V-1\napproval_tier: 0\nrepo: vault\n\n## Intent\nx\n")
    capsys.readouterr()
    assert cli_main(["--home", str(home), "env", "--key", "V-1"]) == 0
    import json
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "local"


# --- AC2 + AC3 + AC4: a real-CLI drive from ready -> implementing -> done ------
# against a non-git temp directory, writing in place with backup-on-write, and
# no worktree/branch/PR anywhere in the flow.

def test_local_mode_ticket_reaches_done_with_backup_on_write(home, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    (target / "note.md").write_text("original\n", encoding="utf-8")
    cfg = _write_local_config(home, target)

    store.atomic_write(
        store.spec_path(home, "V-1"),
        "# V-1\napproval_tier: 0\nrepo: vault\n\n## Intent\nAppend a line to note.md.\n\n"
        "## Acceptance criteria\n- [ ] note.md ends with 'updated'\n",
    )
    event_log.append(home, "V-1", "TicketCreated",
                     {"title": "V-1", "repo": "vault", "spec_hash": "x"}, actor="d")
    snap_mod.rebuild(home, "V-1")
    assert snap_mod.load(home, "V-1").phase == Phase.TRIAGING.value

    # triaging -> ready (tier 0, auto-approved)
    assert cli_main(["--home", str(home), "set-phase", "V-1", "ready",
                     "--reason", "tier-0 auto-approved"]) == 0

    # ready -> implementing: local mode skips the worktree/branch step entirely
    assert cli_main(["--home", str(home), "set-phase", "V-1", "implementing",
                     "--reason", "local target ready"]) == 0
    assert not (home / "worktrees" / "V-1").exists()

    # implementing: back up the target BEFORE writing (idempotent within one step)
    assert cli_main(["--home", str(home), "local-backup", "V-1"]) == 0
    assert cli_main(["--home", str(home), "local-backup", "V-1"]) == 0  # no-op: same step

    backups_dir = target.parent / "vault-backups"
    backups = list(backups_dir.glob("*.tar.gz"))
    assert len(backups) == 1, "local-backup must be idempotent within one reconcile step"
    with tarfile.open(backups[0]) as tar:
        member = tar.extractfile("vault/note.md")
        assert member.read().decode() == "original\n"

    # the reconciler writes directly into the target -- no worktree, no branch
    (target / "note.md").write_text("original\nupdated\n", encoding="utf-8")

    # self-review gate, then finalize straight to done -- no PR, no CI wait
    assert cli_main(["--home", str(home), "verify-ac", "V-1", "--ac", "1",
                     "--evidence", "note.md:2 ends with 'updated'"]) == 0
    assert cli_main(["--home", str(home), "finalize", "V-1"]) == 0

    snap = snap_mod.load(home, "V-1")
    assert snap.phase == Phase.DONE.value
    assert snap.pr_number is None
    assert snap.pr_url is None
    assert not (home / "worktrees" / "V-1").exists()
    assert (target / "note.md").read_text(encoding="utf-8") == "original\nupdated\n"


def test_local_write_backup_is_noop_for_git_mode_binding(cfg):
    home = cfg.home
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 1\n\n## Intent\nx\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1", "spec_hash": "x"}, actor="d")
    snap_mod.rebuild(home, "T-1")
    assert ops.local_write_backup(cfg, "T-1") is None
