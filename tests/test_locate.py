"""maestro/locate.py -- provenance-tagged file/symbol hints for the context
dossier (T-124). Every scenario drives real git repos under `tmp_path` (never
a mocked subprocess) -- the whole point is proving the MENTION/SYMBOL-MAP/
PRIOR-EDIT/churn stages resolve against actual git state.
"""
import json


from maestro import cli, context, event_log, locate, ops, snapshot as snap_mod, store
from maestro.config import Config

from conftest import git as _git, make_origin_and_repo as _make_origin_and_repo


WIDGET_PY = (
    'def build_widget():\n'
    '    """Builds the widget."""\n'
    '    return 1\n'
    '\n'
    '\n'
    'class Widget:\n'
    '    """A widget."""\n'
    '    pass\n'
)


def _seed_widget_repo(tmp_path, name="repo"):
    origin, repo = _make_origin_and_repo(tmp_path, name=name)
    (repo / "maestro").mkdir(exist_ok=True)
    (repo / "maestro" / "widget.py").write_text(WIDGET_PY)
    (repo / "maestro" / "other.py").write_text("def unrelated():\n    return 2\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add widget module", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    return origin, repo


def _add_worktree(repo, home, key, branch="maestro/" + "wt", base="main"):
    wt = store.worktree_path(home, key)
    _git("worktree", "add", "-q", "-b", branch, str(wt), base, cwd=repo)
    return wt


def _write_spec(home, key, intent, acs=("do the thing",)):
    ac_lines = "\n".join(f"- [ ] {t}" for t in acs)
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}: Test\n\npriority: 2\n\n## Intent\n{intent}\n\n"
                       f"## Acceptance criteria\n{ac_lines}\n")


def _create(cfg, key, intent, **kw):
    _write_spec(cfg.home, key, intent, **kw)
    event_log.append(cfg.home, key, "TicketCreated", {"title": "Test", "source": "test"}, actor="d")
    return snap_mod.rebuild(cfg.home, key)


# ---------------------------------------------------------------------------
# `file_hints` config knob: board-wide default + per-repo override
# ---------------------------------------------------------------------------

def test_file_hints_knob_parsed_from_config_toml(home):
    (home / "config.toml").write_text("[maestro]\nfile_hints = true\n", encoding="utf-8")
    from maestro.config import load
    assert load(home).file_hints is True


def test_file_hints_per_repo_override_wins_over_board_default(home):
    (home / "config.toml").write_text(
        "[maestro]\nfile_hints = false\n\n"
        "[repos.other]\npath = \"/tmp/does-not-need-to-exist-x\"\nfile_hints = true\n\n"
        "[repos.mine]\npath = \"/tmp/does-not-need-to-exist-y\"\n",
        encoding="utf-8")
    from maestro.config import load
    from maestro import repos as repos_mod
    cfg = load(home)
    assert repos_mod._binding_from_table(cfg, "other", cfg.repos["other"]).file_hints is True
    assert repos_mod._binding_from_table(cfg, "mine", cfg.repos["mine"]).file_hints is False


# ---------------------------------------------------------------------------
# AC2: `maestro locate <KEY>` writes derived/locate/<KEY>.json
# ---------------------------------------------------------------------------

def test_locate_verb_writes_cache_with_mention_prior_edit_and_symbols(home, tmp_path):
    _origin, repo = _seed_widget_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo))
    key = "T-1"
    _create(cfg, key,
            "Update `maestro/widget.py` (see `widget` module, function `build_widget`) "
            "to support new behavior.")
    _add_worktree(repo, home, key, branch=f"maestro/{key}")
    event_log.append(home, key, "ImplStepRecorded",
                     {"turn": 1, "role": "implementer", "kind": "edit", "tool": "Edit",
                      "summary": "maestro/other.py"}, actor="reconciler")

    rc = cli.main(["--home", str(home), "locate", key])
    assert rc == 0

    cache_file = home / "derived" / "locate" / f"{key}.json"
    assert cache_file.exists()
    result = json.loads(cache_file.read_text(encoding="utf-8"))

    hints_by_path = {h["path"]: h["provenance"] for h in result["hints"]}
    # Path form, stem form ("widget") and backticked-symbol form ("build_widget")
    # all resolve to the same file, deduplicated to one 'mention' row.
    assert hints_by_path["maestro/widget.py"] == "mention"
    assert hints_by_path["maestro/other.py"] == "prior-edit"

    symbols = {(s["name"], s["kind"]): s for s in result["symbols"]}
    assert symbols[("build_widget", "function")]["path"] == "maestro/widget.py"
    assert symbols[("build_widget", "function")]["line_start"] == 1
    assert symbols[("Widget", "class")]["path"] == "maestro/widget.py"
    assert symbols[("Widget", "class")]["docstring"] == "A widget."


def test_locate_verb_requires_a_key_without_eval(home):
    rc = cli.main(["--home", str(home), "locate"])
    assert rc != 0


# ---------------------------------------------------------------------------
# AC3: file_hints=true dossier sections + cache reuse without re-shelling to git
# ---------------------------------------------------------------------------

def test_context_dossier_gains_locate_sections_when_file_hints_on(home, tmp_path, monkeypatch):
    _origin, repo = _seed_widget_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo), file_hints=True)
    key = "T-1"
    _create(cfg, key, "Update `maestro/widget.py`, function `build_widget`.")
    _add_worktree(repo, home, key, branch=f"maestro/{key}")

    ops.locate(cfg, key)  # populates the cache + regenerates the dossier

    text = context.context_path(home, key).read_text(encoding="utf-8")
    assert "## Suggested starting points" in text
    assert "## Symbol map" in text
    assert "## Files edited in prior sessions" in text
    assert "maestro/widget.py" in text
    assert "build_widget" in text

    # Second render for the same (spec_hash, HEAD): context.regenerate only
    # ever reads the cache (`locate.load_cached`), never `locate.compute` --
    # proven here by making any of the expensive git-shelling stages explode.
    def _boom(*a, **kw):
        raise AssertionError("context.regenerate must not re-shell to git")
    monkeypatch.setattr(locate, "resolve_mentions", _boom)
    monkeypatch.setattr(locate, "churn", _boom)

    text2 = context.context_path(home, key).read_text(encoding="utf-8")  # unchanged so far
    context.regenerate(cfg, key)
    text3 = context.context_path(home, key).read_text(encoding="utf-8")
    assert text3 == text2 == text


def test_context_dossier_byte_identical_when_file_hints_off(home, tmp_path):
    """file_hints stays False by default -- the dossier never even loads the
    locate cache (AC1's byte-identical contract lives in test_context.py; this
    proves the repo-binding gate specifically)."""
    _origin, repo = _seed_widget_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo))
    key = "T-1"
    _create(cfg, key, "Update `maestro/widget.py`.")
    _add_worktree(repo, home, key, branch=f"maestro/{key}")
    ops.locate(cfg, key)  # cache exists on disk...

    text = context.context_path(home, key).read_text(encoding="utf-8")
    assert "## Suggested starting points" not in text  # ...but file_hints is off, so unused


def test_locate_compute_reuses_cache_for_same_spec_hash_and_head(home, tmp_path, monkeypatch):
    _origin, repo = _seed_widget_repo(tmp_path)
    cfg = Config(home=home, repo_path=str(repo))
    key = "T-1"
    _create(cfg, key, "Update `maestro/widget.py`.")
    _add_worktree(repo, home, key, branch=f"maestro/{key}")

    first = locate.compute(cfg, key)
    assert first["hints"]

    def _boom(*a, **kw):
        raise AssertionError("compute() must not re-shell to git on an unchanged tree")
    monkeypatch.setattr(locate, "resolve_mentions", _boom)
    monkeypatch.setattr(locate, "churn", _boom)

    second = locate.compute(cfg, key)  # force=False (default): same spec_hash + HEAD
    assert second == first


# ---------------------------------------------------------------------------
# AC4: `maestro locate --eval` replays merged tickets against real history
# ---------------------------------------------------------------------------

def test_locate_eval_reports_stage_metrics_and_exits_zero(home, tmp_path, capsys):
    _origin, repo = _make_origin_and_repo(tmp_path)
    # Seed foo.py/bar.py first -- ranking is over files that existed AT THE
    # PARENT commit (never the working tree), so a brand-new file a ticket's
    # own commit introduces can never be "discovered" that way; each ticket
    # below instead MODIFIES an already-existing file.
    (repo / "foo.py").write_text("def foo():\n    return 0\n")
    (repo / "bar.py").write_text("def bar():\n    return 0\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed foo/bar", cwd=repo)
    (repo / "foo.py").write_text("def foo():\n    return 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-201: update foo module", cwd=repo)
    (repo / "bar.py").write_text("def bar():\n    return 2\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-202: update bar module", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _write_spec(home, "T-201", "Update `foo.py`.")
    _write_spec(home, "T-202", "Update `bar.py`.")
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo)!r}\n", encoding="utf-8")

    rc = cli.main(["--home", str(home), "locate", "--eval"])
    out = capsys.readouterr().out
    result = json.loads(out)
    assert rc == 0
    assert result["commits_evaluated"] == 2
    for stage in ("mention", "prior_edit", "churn", "merged"):
        scores = result["stages"][stage]
        assert set(scores) == {"recall_at_5", "recall_at_10", "mrr"}
    # Both commits' sole changed file is directly named in its own spec.
    assert result["stages"]["mention"]["recall_at_5"] == 1.0
    assert result["baseline_mention_recall_at_5"] == result["stages"]["mention"]["recall_at_5"]


def test_locate_eval_respects_n_cap(home, tmp_path, capsys):
    _origin, repo = _make_origin_and_repo(tmp_path)
    names = ("foo", "bar", "baz")
    for name in names:
        (repo / f"{name}.py").write_text(f"def {name}():\n    return 0\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed foo/bar/baz", cwd=repo)
    for i, name in enumerate(names):
        (repo / f"{name}.py").write_text(f"def {name}():\n    return {i + 1}\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", f"T-30{i}: update {name} module", cwd=repo)
        _write_spec(home, f"T-30{i}", f"Update `{name}.py`.")
    _git("push", "-q", "origin", "main", cwd=repo)
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo)!r}\n", encoding="utf-8")

    rc = cli.main(["--home", str(home), "locate", "--eval", "--n", "1"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["commits_evaluated"] == 1
