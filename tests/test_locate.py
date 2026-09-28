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


# ---------------------------------------------------------------------------
# T-141: strip the `<ref>:` prefix off a grep hit at a historical ref, and
# don't score --eval's truth against files a commit only added.
# ---------------------------------------------------------------------------

def test_resolve_mentions_at_ref_strips_prefix_for_grep_hits(tmp_path):
    _origin, repo = _make_origin_and_repo(tmp_path)
    (repo / "target.py").write_text("def resolve_mentions_probe():\n    return 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add target module", cwd=repo)
    sha = locate._run_git(repo, ["rev-parse", "HEAD"])[0]
    (repo / "unrelated.py").write_text("x = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add unrelated file", cwd=repo)

    # The identifier is only findable via `git grep` -- it's not itself a path,
    # basename or stem of any file -- so this exercises the ref-scoped grep
    # branch for both a full sha and a symbolic ref.
    for ref in (sha, "HEAD~1"):
        hits = locate.resolve_mentions(repo, ["resolve_mentions_probe"], ref=ref)
        assert hits == ["target.py"]
        assert not any(h.startswith(f"{ref}:") for h in hits)


def test_locate_eval_mention_scores_identifier_found_only_via_grep(home, tmp_path, capsys):
    _origin, repo = _make_origin_and_repo(tmp_path)
    (repo / "foo.py").write_text("def probe_symbol():\n    return 0\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed foo", cwd=repo)
    (repo / "foo.py").write_text("def probe_symbol():\n    return 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-210: tweak probe_symbol", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _write_spec(home, "T-210", "Fix `probe_symbol`.")
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo)!r}\n", encoding="utf-8")

    rc = cli.main(["--home", str(home), "locate", "--eval"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    # Before the fix, git grep's ref-scoped output kept the "<parent-sha>:"
    # prefix, so this never matched foo.py and scored 0.0 on every stage.
    assert result["stages"]["mention"]["recall_at_5"] == 1.0
    assert result["stages"]["mention"]["mrr"] == 1.0


def test_locate_eval_excludes_added_files_from_truth(home, tmp_path, capsys):
    _origin, repo = _make_origin_and_repo(tmp_path)
    (repo / "foo.py").write_text("def foo():\n    return 0\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed foo", cwd=repo)
    (repo / "foo.py").write_text("def foo():\n    return 1\n")
    (repo / "new_mod.py").write_text("x = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-211: update foo, add new_mod", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _write_spec(home, "T-211", "Update `foo.py`.")
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo)!r}\n", encoding="utf-8")

    rc = cli.main(["--home", str(home), "locate", "--eval"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    # new_mod.py didn't exist at the parent, so it can never be ranked -- it
    # must not drag mention recall@5 down to 0.5.
    assert result["stages"]["mention"]["recall_at_5"] == 1.0
    row = result["per_commit"][0]
    assert row["key"] == "T-211"
    assert row["excluded_added_files"] == ["new_mod.py"]
    assert result["excluded_added_files_count"] == 1


def test_locate_eval_all_added_commit_matches_averages_of_solo_replay(home, tmp_path, capsys):
    def _seed_and_update(repo):
        (repo / "foo.py").write_text("def foo():\n    return 0\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "seed foo", cwd=repo)
        (repo / "foo.py").write_text("def foo():\n    return 1\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "T-212: update foo module", cwd=repo)

    _write_spec(home, "T-212", "Update `foo.py`.")
    _write_spec(home, "T-213", "Add `brand_new.py`.")

    _origin_a, repo_alone = _make_origin_and_repo(tmp_path, name="alone")
    _seed_and_update(repo_alone)
    _git("push", "-q", "origin", "main", cwd=repo_alone)
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo_alone)!r}\n", encoding="utf-8")
    rc_alone = cli.main(["--home", str(home), "locate", "--eval"])
    result_alone = json.loads(capsys.readouterr().out)

    _origin_b, repo_combo = _make_origin_and_repo(tmp_path, name="combo")
    _seed_and_update(repo_combo)
    (repo_combo / "brand_new.py").write_text("y = 1\n")
    _git("add", "-A", cwd=repo_combo)
    _git("commit", "-q", "-m", "T-213: add brand new module", cwd=repo_combo)
    _git("push", "-q", "origin", "main", cwd=repo_combo)
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo_combo)!r}\n", encoding="utf-8")
    rc_combo = cli.main(["--home", str(home), "locate", "--eval"])
    result_combo = json.loads(capsys.readouterr().out)

    assert rc_alone == 0
    assert rc_combo == 0
    assert result_alone["commits_evaluated"] == 1
    assert result_combo["commits_evaluated"] == 2
    assert result_combo["commits_scored"] == 1
    assert result_combo["commits_excluded_no_prior_changes"] == 1
    by_key = {c["key"]: c for c in result_combo["per_commit"]}
    assert by_key["T-213"]["scored"] is False
    assert by_key["T-212"]["scored"] is True
    # The all-added commit must not shift the averages away from what a solo
    # replay of the one normal commit produces.
    assert result_combo["stages"] == result_alone["stages"]


# ---------------------------------------------------------------------------
# T-142: symbol-level hints -- enclosing def/line-range, not just the file
# ---------------------------------------------------------------------------

def test_locate_symbol_hints_point_at_enclosing_defs_in_hub_file(home, tmp_path):
    """AC1: a spec-backticked identifier used only inside one function of a
    hub-sized module, plus a second function named directly, both surface as
    enclosing defs with exact line ranges -- and no other def of that module
    (the module has 40 unrelated padding functions the spec never mentions)."""
    _origin, repo = _make_origin_and_repo(tmp_path)
    (repo / "maestro").mkdir(exist_ok=True)
    padding = "".join(
        f"def padding_fn_{i}():\n    x = {i}\n    y = x + 1\n    return y\n\n\n"
        for i in range(40)
    )
    hub_src = (
        padding
        + "def function_a():\n    return probe_only_here()\n\n\n"
        + "def function_b():\n    return 2\n"
    )
    assert hub_src.count("\n") + 1 >= locate.HUB_FILE_LINE_THRESHOLD
    (repo / "maestro" / "hub_module.py").write_text(hub_src)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add hub module", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    cfg = Config(home=home, repo_path=str(repo), file_hints=True)
    key = "T-1"
    _create(cfg, key,
            "In `maestro/hub_module.py`, fix `probe_only_here` and check `function_b`.")
    _add_worktree(repo, home, key, branch=f"maestro/{key}")

    ops.locate(cfg, key)
    text = context.context_path(home, key).read_text(encoding="utf-8")
    assert "maestro/hub_module.py:" in text
    assert "function_a" in text
    assert "function_b" in text
    assert "padding_fn_" not in text


def test_locate_symbol_cap_drops_lowest_ranked_not_earliest_walked(home, tmp_path):
    """AC2: a hub module with 31 spec-matched defs -- 30 pinned by an exact
    `path:line` spec reference (including the very first def in the file) and
    1 matched only via a weaker body-reference -- exceeds the 30-row symbol
    cap by exactly one. The cap must drop the weakest match (the last def,
    lowest-ranked), not whichever def the old last-N-walked cap would have
    dropped (which was the FIRST def, since it's walked before the other 30)."""
    _origin, repo = _make_origin_and_repo(tmp_path)
    (repo / "maestro").mkdir(exist_ok=True)

    def block(name, body_lines):
        return f"def {name}():\n" + "".join(f"    {b}\n" for b in body_lines) + "\n"

    n_hinted = 30
    pieces = []
    line_hints = []
    current_line = 1
    for i in range(n_hinted):
        name = f"ranked_fn_{i}"
        text = block(name, ["a = 1", "b = 2", "c = 3", "d = 4", "return a + b + c + d"])
        pieces.append(text)
        line_hints.append((name, current_line))
        current_line += text.count("\n")
    tail_name = "unranked_tail_fn"
    pieces.append(block(tail_name, ["return shared_helper()"]))

    src = "".join(pieces)
    assert src.count("\n") + 1 >= locate.HUB_FILE_LINE_THRESHOLD
    (repo / "maestro" / "hub_cap.py").write_text(src)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "add hub_cap module", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    hint_refs = " ".join(f"`maestro/hub_cap.py:{ln}`" for _, ln in line_hints)
    intent = f"In `maestro/hub_cap.py`, fix `shared_helper` call sites. {hint_refs}"

    cfg = Config(home=home, repo_path=str(repo), file_hints=True)
    key = "T-1"
    _create(cfg, key, intent)
    _add_worktree(repo, home, key, branch=f"maestro/{key}")

    ops.locate(cfg, key)
    result = json.loads((home / "derived" / "locate" / f"{key}.json").read_text(encoding="utf-8"))
    names = {s["name"] for s in result["symbols"]}

    assert len(result["symbols"]) == 30
    assert result["symbols_dropped"] == 1
    assert all(f"ranked_fn_{i}" in names for i in range(n_hinted))  # top-of-file one included
    assert tail_name not in names  # lowest-ranked (weakest match) dropped by the cap


# ---------------------------------------------------------------------------
# AC3: `locate --eval` symbol-level recall@5/10/MRR (new ranking + baseline)
# ---------------------------------------------------------------------------

def test_locate_eval_reports_symbol_level_metrics(home, tmp_path, capsys):
    _origin, repo = _make_origin_and_repo(tmp_path)
    mod_src = (
        "def alpha():\n    return 0\n\n\n"
        "def beta():\n    return 0\n\n\n"
        "def target_fn():\n    return 0\n"
    )
    (repo / "mod.py").write_text(mod_src)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "seed mod", cwd=repo)
    (repo / "mod.py").write_text(
        mod_src.replace("def target_fn():\n    return 0\n", "def target_fn():\n    return 1\n"))
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "T-300: fix target_fn", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)

    _write_spec(home, "T-300", "Fix `target_fn`.")
    (home / "config.toml").write_text(f"[maestro]\nrepo_path = {str(repo)!r}\n", encoding="utf-8")

    rc = cli.main(["--home", str(home), "locate", "--eval"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    # Existing file-level stage keys are unchanged.
    for stage in ("mention", "prior_edit", "churn", "merged"):
        assert set(result["stages"][stage]) == {"recall_at_5", "recall_at_10", "mrr"}
    for stage in ("symbol_new", "symbol_baseline"):
        assert set(result["stages"][stage]) == {"recall_at_5", "recall_at_10", "mrr"}
    assert result["stages"]["symbol_new"]["recall_at_5"] == 1.0
    assert result["stages"]["symbol_new"]["mrr"] == 1.0
    row = result["per_commit"][0]
    assert row["symbol_scored"] is True
    assert set(row["symbol_scores"]["symbol_new"]) == {"recall_at_5", "recall_at_10", "mrr"}

    # A working-tree edit made after the replayed commits leaves the scores
    # unchanged -- symbol content, like file content, always comes from the
    # PARENT commit, never the working tree.
    (repo / "mod.py").write_text("def target_fn():\n    return 999\n")
    rc2 = cli.main(["--home", str(home), "locate", "--eval"])
    result2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert result2["stages"] == result["stages"]
