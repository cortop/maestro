"""T-218: the out-of-scope-paths question also offers `allow` / `allow: <glob>`;
the dispatcher (never an agent) applies it to config.toml."""
from __future__ import annotations

from pathlib import Path

import pytest

from maestro import cli, config as config_mod, dispatcher as disp, event_log, ops
from maestro import repos as repos_mod, snapshot as snap_mod, store
from maestro.sessions import DryRunSessions
from maestro.statemachine import Phase

from conftest import make_origin_and_repo
from test_dispatcher_ac_checks import (
    _PASS_CMD, _advance_to_verifying, _commit_test_file, _run_verifying_to_completion,
    _seed_to_worktree,
)

KEY = "G-1"
HEADER = "# my board\n[maestro]\n# keep me\nmax_concurrency = 3\n\n"


def _write(home, repo, repo_extra='allowed_paths = ["src/**"]  # scope\n', maestro_extra=""):
    text = (HEADER + maestro_extra + f'[repos.default]\npath = "{repo}"\ndefault = true\n'
            f"test_command = '{_PASS_CMD}'\n{repo_extra}\n# tail comment\n")
    (home / "config.toml").write_text(text)
    return text


def _load(home):
    return config_mod.load(str(home))


def _asked(home):
    return [e["payload"] for e in event_log.read(home, KEY) if e["type"] == "QuestionAsked"]


def _answer(home, monkeypatch, text, qid):
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    assert cli.main(["ans", KEY, text, "--qid", qid, "--no-nudge"]) == 0


def _to_question(tmp_path, home, files=("docs/one.md", "ops/two.sh"), **kw):
    _origin, repo = make_origin_and_repo(tmp_path)
    before = _write(home, repo, **kw)
    cfg = _load(home)
    wt = _seed_to_worktree(cfg, KEY, acs=["x"])
    _commit_test_file(wt, "src/a.py", "x = 1\n")
    for f in files:
        _commit_test_file(wt, f, "1\n")
    _advance_to_verifying(cfg, KEY)
    _run_verifying_to_completion(cfg, KEY, DryRunSessions())
    assert snap_mod.load(home, KEY).phase == Phase.AWAITING_HUMAN.value
    (q,) = _asked(home)
    return cfg, wt, q, before


def _sweep(home, now):
    return disp.dispatch(_load(home), DryRunSessions(), now=now)


def test_both_questions_offer_approve_allow_or_revert(tmp_path, home):
    _cfg, _wt, q, _ = _to_question(tmp_path, home)
    for needle in ("approve", "`allow`", "`allow: <glob>`", "reverted", "[files: docs/one.md, ops/two.sh]"):
        assert needle in q["text"]
    skill = (Path(__file__).parent.parent / ".claude/commands/maestro-reconcile-implementing.md").read_text()
    for needle in ("`approve`", "`allow`", "`allow: <glob>`", "[files:", "scope-$KEY-<n>"):
        assert needle in skill


def test_allow_adds_listed_files_and_gate_then_passes(tmp_path, home, monkeypatch):
    cfg, wt, q, before = _to_question(tmp_path, home)
    _answer(home, monkeypatch, "allow", q["qid"])
    _sweep(home, 2000)
    after = (home / "config.toml").read_text()
    assert after == before.replace('["src/**"]', '["src/**", "docs/one.md", "ops/two.sh"]')
    assert _load(home).repos["default"]["allowed_paths"] == ["src/**", "docs/one.md", "ops/two.sh"]
    binding = repos_mod.resolve(_load(home), home, KEY)
    assert binding.allowed_paths == ["src/**", "docs/one.md", "ops/two.sh"]
    assert snap_mod.load(home, KEY).phase in (Phase.VERIFYING.value, Phase.QA.value)

    for now in (2100, 2200):  # the suite result for this tree is cached
        if snap_mod.load(home, KEY).phase == Phase.QA.value:
            break
        _sweep(home, now)
    assert snap_mod.load(home, KEY).phase == Phase.QA.value
    assert len(_asked(home)) == 1

    # a new outside file still asks
    ops.set_phase(_load(home), KEY, Phase.IMPLEMENTING, reason="fix")
    _commit_test_file(wt, "docs/three.md", "3\n")
    ops.set_phase(_load(home), KEY, Phase.QA, reason="pr opened")
    _run_verifying_to_completion(_load(home), KEY, DryRunSessions())
    qs = _asked(home)
    assert len(qs) == 2 and "[files: docs/three.md]" in qs[1]["text"]


def test_allow_is_event_logged_once_and_idempotent(tmp_path, home, monkeypatch):
    _cfg, _wt, q, _ = _to_question(tmp_path, home)
    _answer(home, monkeypatch, "allow", q["qid"])
    _sweep(home, 2000)
    _sweep(home, 2100)
    cfg = _load(home)
    # crash retry: the same call again adds nothing and appends nothing
    assert ops.add_allowed_paths(cfg, KEY, q["qid"], ["docs/one.md", "ops/two.sh"]) == []
    ev = [e for e in event_log.read(home, KEY) if e["type"] == "AllowedPathsAdded"]
    assert len(ev) == 1
    assert ev[0]["payload"] == {"qid": q["qid"], "table": "repos.default",
                                "paths": ["docs/one.md", "ops/two.sh"]}
    assert cfg.repos["default"]["allowed_paths"].count("docs/one.md") == 1


def test_allow_glob_adds_glob_instead(tmp_path, home, monkeypatch):
    _cfg, _wt, q, before = _to_question(tmp_path, home)
    _answer(home, monkeypatch, "allow: docs/**", q["qid"])
    _sweep(home, 2000)
    assert (home / "config.toml").read_text() == before.replace('["src/**"]', '["src/**", "docs/**"]')


@pytest.mark.parametrize("bad", ["allow:", "allow: ../x/**", "allow: /etc/*", "allow: a b", "allow: **"])
def test_malformed_glob_is_refused_without_writing(tmp_path, home, monkeypatch, bad):
    _cfg, _wt, q, before = _to_question(tmp_path, home)
    _answer(home, monkeypatch, bad, q["qid"])
    _sweep(home, 2000)
    assert (home / "config.toml").read_text() == before
    assert not [e for e in event_log.read(home, KEY) if e["type"] == "AllowedPathsAdded"]
    snap = snap_mod.load(home, KEY)
    assert snap.phase == Phase.AWAITING_HUMAN.value
    (retry,) = snap.open_questions
    assert retry.startswith(q["qid"]) and retry != q["qid"]
    assert "Refused" in snap.open_questions[retry] and "[files:" in snap.open_questions[retry]


@pytest.mark.parametrize("text", ["approved", "yes", "allow it but only for this ticket"])
def test_plain_approval_leaves_config_unchanged(tmp_path, home, monkeypatch, text):
    _cfg, _wt, q, before = _to_question(tmp_path, home)
    _answer(home, monkeypatch, text, q["qid"])
    sessions = DryRunSessions()
    disp.dispatch(_load(home), sessions, now=2000)
    assert (home / "config.toml").read_text() == before
    assert not [e for e in event_log.read(home, KEY) if e["type"] == "AllowedPathsAdded"]
    assert any(k == KEY for k, *_ in sessions.spawned) or snap_mod.load(home, KEY).inbox_pending


def test_fast_path_never_treats_scope_answer_as_approval(tmp_path, home, monkeypatch):
    _cfg, _wt, q, _ = _to_question(tmp_path, home)
    _answer(home, monkeypatch, "yes", q["qid"])
    snap = snap_mod.load(home, KEY)
    assert disp._answer_fast_path_eligible(home, KEY, snap, "inbox") is None
    assert q["qid"].startswith(disp.SCOPE_QID_PREFIXES)


def test_no_agent_verb_can_write_the_allowlist():
    names = " ".join(disp.AGENT_TOOL_VERBS)
    assert "allow" not in names and "config" not in names
    parser_text = cli.build_parser().format_help()
    assert "allowed" not in parser_text


def test_unlisted_repo_list_inherits_board_wide_entries(tmp_path, home):
    _origin, repo = make_origin_and_repo(tmp_path)
    text = _write(home, repo, repo_extra="", maestro_extra='allowed_paths = ["maestro/**"]\n')
    cfg = _load(home)
    store.atomic_write(store.spec_path(home, KEY), f"# {KEY}\n\n## Acceptance criteria\n- [ ] x\n")
    event_log.append(home, KEY, "TicketCreated", {"title": KEY, "source": "t",
                     "spec_hash": disp.spec_hash_on_disk(home, KEY)}, actor="d")
    snap_mod.rebuild(home, KEY)
    assert ops.add_allowed_paths(cfg, KEY, "scope-G-1-1", ["docs/x.md"]) == ["docs/x.md"]
    new = _load(home)
    assert new.repos["default"]["allowed_paths"] == ["maestro/**", "docs/x.md"]
    assert new.allowed_paths == ["maestro/**"]
    assert repos_mod.resolve(new, home, KEY).allowed_paths == ["maestro/**", "docs/x.md"]
    assert (home / "config.toml").read_text() == text.replace(
        "[repos.default]\n", '[repos.default]\nallowed_paths = ["maestro/**", "docs/x.md"]\n', 1)


def test_no_binding_writes_maestro_table(tmp_path, home):
    (home / "config.toml").write_text("[maestro]\nmax_concurrency = 2  # c\n[vcs]\nx = 1\n")
    cfg = _load(home)
    assert config_mod.add_allowed_paths(home, None, ["a/**"], cfg.allowed_paths) == ["a/**"]
    assert (home / "config.toml").read_text() == (
        '[maestro]\nallowed_paths = ["a/**"]\nmax_concurrency = 2  # c\n[vcs]\nx = 1\n')
    assert _load(home).allowed_paths == ["a/**"]


def test_multiline_array_and_symlink_survive(tmp_path, home):
    real = tmp_path / "real.toml"
    real.write_text('[maestro]\nallowed_paths = [\n  "a/**",  # first\n  "b/**",\n]\nmax_concurrency = 2\n')
    (home / "config.toml").unlink()
    (home / "config.toml").symlink_to(real)
    assert config_mod.add_allowed_paths(home, None, ["c/**", "a/**"], ["a/**", "b/**"]) == ["c/**"]
    assert (home / "config.toml").is_symlink()
    assert real.read_text() == '[maestro]\nallowed_paths = ["a/**", "b/**", "c/**"]\nmax_concurrency = 2\n'


def test_awaiting_human_skill_routes_scope_qids():
    text = (Path(__file__).parent.parent / ".claude/commands/maestro-reconcile-awaiting-human.md").read_text()
    assert "`allowed-paths-`" in text and "`scope-`" in text and "dispatcher itself applies" in text
