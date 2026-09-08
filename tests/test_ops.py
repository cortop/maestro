"""T-112: `ops.add_ac` (the one write-through) and the `maestro add-ac <KEY>
<text>` CLI verb that calls it -- appending a new `- [ ] <text>` line to a
ticket's spec `## Acceptance criteria` section from outside the TUI. Mirrors
`test_runner_edit.py`'s shape for the `ops.set_runner` / UX-1 precedent.

T-113: `ops.suggest_acs` -- the read-only, bounded `claude -p` capture call
that drafts candidate ACs for an AC-less ticket; `run=` is mocked as the
external boundary, same convention as `providers/cli.py._run`.
"""
from __future__ import annotations

import difflib
import json
import subprocess

import pytest

from maestro import event_log, ops, snapshot as snap_mod, store
from maestro.cli import main as cli_main

SPEC = (
    "# T-1: Sample\n"
    "\n"
    "<!-- HUMAN-OWNED. Edit freely, anytime. Agents read this; they never rewrite it. -->\n"
    "\n"
    "priority: 3\n"
    "dependsOn: []\n"
    "\n"
    "## Intent\n"
    "Do the thing.\n"
    "\n"
    "## Acceptance criteria\n"
    "- [ ] it works\n"
)

# The exact shape `cli._SEED_SPEC_TEMPLATE` leaves at ticket minting: a
# dangling "- [ ] " placeholder with no trailing newline, right at EOF.
SEED_SPEC = (
    "# T-1: Sample\n"
    "\n"
    "priority: 3\n"
    "dependsOn: []\n"
    "\n"
    "## Intent\n"
    "Do the thing.\n"
    "\n"
    "## Acceptance criteria\n"
    "- [ ] "
)

NO_AC_SECTION_SPEC = (
    "# T-1: Sample\n"
    "\n"
    "priority: 3\n"
    "\n"
    "## Intent\n"
    "Do the thing.\n"
)


def _seed_ticket(home, key, *, spec=SPEC):
    store.atomic_write(store.spec_path(home, key), spec)
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": "x"}, actor="d")
    snap_mod.rebuild(home, key)


# --- ops.add_ac -------------------------------------------------------------

def test_add_ac_appends_after_existing_ac(home, cfg):
    _seed_ticket(home, "T-1")
    spec_path = store.spec_path(home, "T-1")
    before = spec_path.read_text()

    result = ops.add_ac(cfg, "T-1", "the new thing works")

    after = spec_path.read_text()
    assert result == {"text": "the new thing works"}
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
    added = [l for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
    assert added == ["+- [ ] the new thing works"]
    assert removed == []
    assert snap_mod.parse_acs(after) == ["it works", "the new thing works"]


def test_add_ac_preserves_seed_templates_blank_placeholder_line(home, cfg):
    """T-112 Q4: the seed template's own dangling '- [ ] ' placeholder (no
    trailing newline, at EOF) is left in place -- the new AC is just
    appended after it, never special-cased."""
    _seed_ticket(home, "T-1", spec=SEED_SPEC)

    ops.add_ac(cfg, "T-1", "a real AC")

    after = store.spec_path(home, "T-1").read_text()
    assert after.endswith("## Acceptance criteria\n- [ ] \n- [ ] a real AC\n")
    # the blank placeholder still parses to an empty entry, exactly as before
    assert snap_mod.parse_acs(after) == ["", "a real AC"]


def test_add_ac_creates_section_if_absent(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    ops.add_ac(cfg, "T-1", "first AC")

    after = store.spec_path(home, "T-1").read_text()
    assert "Do the thing." in after  # every prior byte preserved
    assert after.endswith("## Acceptance criteria\n- [ ] first AC\n")
    assert snap_mod.parse_acs(after) == ["first AC"]


def test_add_ac_strips_and_rejects_blank_text(home, cfg):
    _seed_ticket(home, "T-1")
    before = store.spec_path(home, "T-1").read_bytes()

    with pytest.raises(store.MaestroError):
        ops.add_ac(cfg, "T-1", "   ")

    assert store.spec_path(home, "T-1").read_bytes() == before


def test_add_ac_preserves_trailing_annotation_verbatim(home, cfg):
    _seed_ticket(home, "T-1")

    ops.add_ac(cfg, "T-1", "it does the thing (test: tests/test_x.py::test_it)")

    after = store.spec_path(home, "T-1").read_text()
    assert "- [ ] it does the thing (test: tests/test_x.py::test_it)" in after


def test_add_ac_appends_no_event(home, cfg):
    _seed_ticket(home, "T-1")
    before = event_log.read(home, "T-1")

    ops.add_ac(cfg, "T-1", "another AC")

    assert event_log.read(home, "T-1") == before


def test_add_ac_rejects_unknown_key(home, cfg):
    with pytest.raises(store.MaestroError):
        ops.add_ac(cfg, "NOPE", "some AC")


# --- CLI `maestro add-ac <KEY> <text>` --------------------------------------

def test_cli_add_ac_appends_and_snapshot_parse_acs_sees_it(home):
    _seed_ticket(home, "T-1")

    rc = cli_main(["--home", str(home), "add-ac", "T-1", "a cli-added AC"])

    assert rc == 0
    after = store.spec_path(home, "T-1").read_text()
    assert snap_mod.parse_acs(after) == ["it works", "a cli-added AC"]


def test_cli_add_ac_rejects_blank_text(home, capsys):
    _seed_ticket(home, "T-1")

    rc = cli_main(["--home", str(home), "add-ac", "T-1", "  "])

    assert rc != 0
    err = capsys.readouterr().err
    assert "error:" in err


def test_cli_add_ac_rejects_unknown_key(home, capsys):
    rc = cli_main(["--home", str(home), "add-ac", "NOPE", "some AC"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "error:" in err


# --- ops.suggest_acs (T-113) -------------------------------------------------
# `run=` is the injectable external boundary (the `providers/cli.py._run`
# convention) -- these tests never spawn a real `claude` process.

def _fake_run(*, returncode=0, stdout="", stderr=""):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=returncode,
                                           stdout=stdout, stderr=stderr)
    return run


def test_suggest_acs_parses_json_envelope_result(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)
    envelope = json.dumps({"type": "result", "subtype": "success",
                           "result": json.dumps(["AC one", "AC two"])})

    suggestions = ops.suggest_acs(cfg, "T-1", run=_fake_run(stdout=envelope))

    assert suggestions == ["AC one", "AC two"]


def test_suggest_acs_parses_bare_json_array(home, cfg):
    """Tolerates a fake/future run= that hands back the array directly,
    with no `--output-format json` envelope wrapper."""
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    suggestions = ops.suggest_acs(
        cfg, "T-1", run=_fake_run(stdout=json.dumps(["a thing works"])))

    assert suggestions == ["a thing works"]


def test_suggest_acs_strips_and_drops_blank_entries(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)
    envelope = json.dumps({"result": json.dumps(["  padded  ", "", "   ", "ok"])})

    suggestions = ops.suggest_acs(cfg, "T-1", run=_fake_run(stdout=envelope))

    assert suggestions == ["padded", "ok"]


def test_suggest_acs_includes_reconcile_model_and_spec_in_prompt(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)
    seen_cmd = {}

    def run(cmd, **kwargs):
        seen_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=json.dumps(["ok"]))

    ops.suggest_acs(cfg, "T-1", run=run)

    cmd = seen_cmd["cmd"]
    assert cmd[0:2] == ["claude", "-p"]
    assert "Do the thing." in cmd[2]  # the spec text rode along in the prompt
    assert cfg.reconcile_model in cmd
    assert "--output-format" in cmd and "json" in cmd


def test_suggest_acs_raises_on_nonzero_exit(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "T-1", run=_fake_run(returncode=1, stderr="boom"))


def test_suggest_acs_raises_on_unparseable_output(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "T-1", run=_fake_run(stdout="not json at all"))


def test_suggest_acs_raises_on_empty_suggestion_list(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "T-1", run=_fake_run(stdout=json.dumps({"result": "[]"})))


def test_suggest_acs_raises_on_timeout(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "T-1", run=run)


def test_suggest_acs_raises_when_claude_not_found(home, cfg):
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)

    def run(cmd, **kwargs):
        raise FileNotFoundError("claude")

    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "T-1", run=run)


def test_suggest_acs_rejects_missing_spec(home, cfg):
    with pytest.raises(store.MaestroError):
        ops.suggest_acs(cfg, "NOPE", run=_fake_run(stdout=json.dumps(["ok"])))


def test_suggest_acs_never_writes_spec(home, cfg):
    """Suggesting is read-only -- only a later `ops.add_ac` call writes."""
    _seed_ticket(home, "T-1", spec=NO_AC_SECTION_SPEC)
    before = store.spec_path(home, "T-1").read_bytes()

    ops.suggest_acs(cfg, "T-1", run=_fake_run(stdout=json.dumps(["ok"])))

    assert store.spec_path(home, "T-1").read_bytes() == before
