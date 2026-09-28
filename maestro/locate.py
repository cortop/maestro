"""Provenance-tagged file/symbol hints for a ticket's context dossier (T-124).

A replay over 42 merged tickets (docs/spike-laya.md) showed the cheapest ranker
wins: files the spec literally names reach recall@5 0.48 / MRR 0.82, beating
BM25 (0.38) and a 421M decision model (0.17). This module computes that MENTION
stage plus three cheap complements -- an AST symbol map, this ticket's own
prior-edit history (folded from its event log, zero git cost), and git churn as
a tiebreak -- and caches the merged result under ``derived/locate/<KEY>.json``
keyed by (spec_hash, HEAD) so a render between edits never re-shells to git.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from . import event_log, store
from . import events as E
from .config import Config
from .dispatcher import spec_hash_on_disk

MAX_HINTS = 15
MAX_SYMBOL_FILES = 8
MAX_SYMBOL_ROWS = 30
MAX_CHURN_COMMITS = 200
MAX_GREP_CANDIDATES = 25

# T-142: a file at/above this many lines is a "hub" file (dispatcher.py/ops.py/
# cli.py are 1.9k-4.6k lines; the smallest fixture in test_locate.py is well
# under it) -- for these, the dossier lists only spec-matched defs instead of
# the whole module. Picked, and documented here rather than as a config knob,
# because Notes explicitly scopes this to "pick it and document it in the PR".
HUB_FILE_LINE_THRESHOLD = 200

_GIT_TIMEOUT = 20

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATHLIKE_RE = re.compile(r"\b[\w][\w./-]*\.[A-Za-z][\w]{0,8}\b")
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_LINE_TOKEN_RE = re.compile(r"^([\w./-]+\.[A-Za-z][\w]*):(\d+)(?:-(\d+))?$")


def _run_git(repo: Path, args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                               text=True, timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def extract_mentions(spec_text: str) -> list[str]:
    """Candidate path/basename/stem/backticked-identifier strings named in
    *spec_text* -- purely textual, no filesystem/git access. Order-preserving,
    de-duplicated."""
    seen: dict[str, None] = {}
    for m in _PATHLIKE_RE.finditer(spec_text):
        seen.setdefault(m.group(0), None)
    for m in _BACKTICK_RE.finditer(spec_text):
        token = m.group(1).strip()
        # Trim call-syntax/line-range noise off a backticked reference, e.g.
        # "`cmd_x()`" or "`maestro/context.py:41`" -> the bare path/identifier.
        token = token.split("(")[0].split(":")[0].strip()
        if token and " " not in token and not token.startswith(("--", "-")):
            seen.setdefault(token, None)
    return list(seen)


def extract_path_line_mentions(spec_text: str) -> dict[str, set[int]]:
    """path -> exact line numbers named by a backticked `path:line` or
    `path:start-end` spec reference. `extract_mentions` throws the `:line`
    part of such a token away; here it's kept, because when a spec cites a
    line, that line is an exact symbol hint for free (T-142)."""
    out: dict[str, set[int]] = {}
    for m in _BACKTICK_RE.finditer(spec_text):
        token = m.group(1).strip().split("(")[0].strip()
        pm = _PATH_LINE_TOKEN_RE.match(token)
        if not pm:
            continue
        path, start, end = pm.group(1), int(pm.group(2)), pm.group(3)
        lines = out.setdefault(path, set())
        if end:
            lines.update(range(start, int(end) + 1))
        else:
            lines.add(start)
    return out


def _list_files(repo: Path, ref: str | None) -> list[str]:
    if ref:
        return _run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    return _run_git(repo, ["ls-files"])


def _grep_word(repo: Path, term: str, ref: str | None) -> list[str]:
    if ref:
        prefix = f"{ref}:"
        lines = _run_git(repo, ["grep", "-lw", "-I", term, ref, "--"])
        return [line[len(prefix):] if line.startswith(prefix) else line for line in lines]
    return _run_git(repo, ["grep", "-lw", "-I", "--", term])


def resolve_mentions(repo: Path, candidates: list[str], *, ref: str | None = None) -> list[str]:
    """Repo files matching *candidates* by path, basename, or stem (against
    `git ls-files`/`git ls-tree`), or as a whole-word literal (against `git
    grep -lw`) -- provenance 'mention'. *ref* pins both to a historical commit
    instead of the working tree (used by the eval harness). Order:
    first-matched-candidate order, de-duplicated file paths.
    """
    if not candidates:
        return []
    all_files = _list_files(repo, ref)
    if not all_files:
        return []
    by_basename: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for f in all_files:
        base = f.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        by_basename.setdefault(base, []).append(f)
        by_stem.setdefault(stem, []).append(f)
    file_set = set(all_files)

    matched: dict[str, None] = {}
    grep_calls = 0
    for cand in candidates:
        cand_clean = cand.strip("`").strip().lstrip("./")
        if not cand_clean:
            continue
        if cand_clean in file_set:
            matched.setdefault(cand_clean, None)
            continue
        hit = False
        for f in by_basename.get(cand_clean, []):
            matched.setdefault(f, None)
            hit = True
        stem = cand_clean.rsplit(".", 1)[0] if "." in cand_clean else cand_clean
        for f in by_stem.get(stem, []):
            matched.setdefault(f, None)
            hit = True
        # A bare identifier (typically a backticked symbol, e.g. `render`) that
        # isn't itself a path/basename/stem match is checked as a whole-word
        # literal via `git grep` -- bounded so an identifier-heavy spec can't
        # trigger unbounded subprocess fan-out.
        if not hit and grep_calls < MAX_GREP_CANDIDATES and _BARE_IDENTIFIER_RE.match(cand_clean):
            grep_calls += 1
            for f in _grep_word(repo, cand_clean, ref):
                if f in file_set:
                    matched.setdefault(f, None)
    return list(matched)


def prior_edits(home: Path, key: str) -> list[str]:
    """Files this ticket's own earlier sessions edited -- folded from
    ``ImplStepRecorded`` events (kind ``edit``, summary = the file path).
    Pure fold, zero git cost. Order-preserving, de-duplicated."""
    seen: dict[str, None] = {}
    for ev in event_log.read(home, key):
        if ev.get("type") != E.IMPL_STEP:
            continue
        p = ev.get("payload") or {}
        if p.get("kind") == "edit" and p.get("summary"):
            seen.setdefault(p["summary"], None)
    return list(seen)


def churn(repo: Path, *, ref: str | None = None, limit_commits: int = MAX_CHURN_COMMITS) -> list[str]:
    """Files touched most often in the last *limit_commits* commits reachable
    from *ref* (default: the working tree's HEAD) -- the tiebreak stage."""
    base = ref or "HEAD"
    out = _run_git(repo, ["log", base, f"-n{limit_commits}", "--name-only", "--pretty=format:"])
    counts: dict[str, int] = {}
    for line in out:
        line = line.strip()
        if line:
            counts[line] = counts.get(line, 0) + 1
    return [f for f, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def merge_hints(mention: list[str], prior_edit: list[str], churn_files: list[str],
                 *, cap: int = MAX_HINTS) -> list[dict]:
    """Ordered-provenance merge (A, C, then D fills), capped -- 'the cheapest
    ranker wins' stays the top of the list regardless of how large a later
    stage's own candidate pool is."""
    rows: list[dict] = []
    seen: set[str] = set()
    for f in mention:
        if len(rows) >= cap:
            break
        if f not in seen:
            rows.append({"path": f, "provenance": "mention"})
            seen.add(f)
    for f in prior_edit:
        if len(rows) >= cap:
            break
        if f not in seen:
            rows.append({"path": f, "provenance": "prior-edit"})
            seen.add(f)
    for f in churn_files:
        if len(rows) >= cap:
            break
        if f not in seen:
            rows.append({"path": f, "provenance": "churn"})
            seen.add(f)
    return rows


def _read_file_at(repo: Path, rel: str, ref: str | None) -> str | None:
    """*rel*'s text at *ref*, or in the working tree when *ref* is None.
    None on any read/decode failure -- callers treat a file they can't read as
    absent, never an error (fail-open, same contract as the old bare
    ``path.read_text()`` call this replaces)."""
    if ref is None:
        try:
            return (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def symbol_map(repo: Path, files: list[str], *, ref: str | None = None) -> list[dict]:
    """One `ast` walk per top Python file -- def/class name -> path, line
    range, first docstring line -- so a session can `Read` with offset/limit
    instead of re-reading the whole file. Non-.py files, and any file that
    fails to parse, are silently skipped (fail-open -- a symbol map is a
    convenience, never a requirement). *ref* reads each file's content at that
    commit instead of the working tree -- used by `run_eval` to replay this
    exact, unfiltered, untruncated ordering as T-142's baseline stage."""
    symbols: list[dict] = []
    for rel in files[:MAX_SYMBOL_FILES]:
        if not rel.endswith(".py"):
            continue
        text = _read_file_at(repo, rel, ref)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node) or ""
                first_line = doc.splitlines()[0] if doc else ""
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                symbols.append({
                    "path": rel,
                    "name": node.name,
                    "kind": kind,
                    "line_start": node.lineno,
                    "line_end": getattr(node, "end_lineno", node.lineno),
                    "docstring": first_line,
                })
    return symbols


def _ast_symbols(text: str, rel: str) -> list[dict]:
    """Every def/class in *text*, nested included, qualified by its enclosing
    class/def chain (``Class.method``) -- the shared building block for both
    T-142's ranked symbol map and its diff-hunk symbol truth, so the two use
    an identical notion of "which symbol is this line inside"."""
    try:
        tree = ast.parse(text, filename=rel)
    except (SyntaxError, ValueError):
        return []
    out: list[dict] = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                doc = ast.get_docstring(child) or ""
                out.append({
                    "path": rel,
                    "name": child.name,
                    "qualname": qualname,
                    "kind": kind,
                    "line_start": child.lineno,
                    "line_end": getattr(child, "end_lineno", child.lineno),
                    "docstring": doc.splitlines()[0] if doc else "",
                })
                walk(child, qualname)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _grep_word_lines(repo: Path, term: str, ref: str | None) -> list[tuple[str, int]]:
    """(path, line) hits for whole-word literal *term*, ref-scoped like
    `_grep_word` -- with the same `<ref>:` prefix strip T-141 added there."""
    if ref:
        prefix = f"{ref}:"
        raw = _run_git(repo, ["grep", "-nw", "-I", term, ref, "--"])
        raw = [line[len(prefix):] if line.startswith(prefix) else line for line in raw]
    else:
        raw = _run_git(repo, ["grep", "-nw", "-I", "--", term])
    hits: list[tuple[str, int]] = []
    for line in raw:
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        try:
            hits.append((parts[0], int(parts[1])))
        except ValueError:
            continue
    return hits


def _symbol_rank(name: str, line_start: int, line_end: int, path: str,
                  candidates: set[str], grep_hit_lines: dict[str, set[int]],
                  path_line_hints: dict[str, set[int]]) -> int:
    """0: an explicit `path:line` spec reference falls inside this def -- an
    exact hint, free. 1: the def's own name is itself a spec term. 2: a spec
    identifier is used somewhere in the def's body (`git grep -nw`). 3:
    unmatched."""
    hinted = path_line_hints.get(path)
    if hinted and any(line_start <= ln <= line_end for ln in hinted):
        return 0
    if name in candidates:
        return 1
    hits = grep_hit_lines.get(path)
    if hits and any(line_start <= ln <= line_end for ln in hits):
        return 2
    return 3


def ranked_symbol_map(repo: Path, files: list[str], candidates: list[str],
                       path_line_hints: dict[str, set[int]], *, ref: str | None = None,
                       hub_threshold: int = HUB_FILE_LINE_THRESHOLD) -> list[dict]:
    """T-142: like `symbol_map`, but (a) for a file at/above *hub_threshold*
    lines, keeps only the defs the spec actually points at instead of the
    whole module, and (b) globally ranks an exact `path:line` hit first, a
    name match second, a body-reference match third, everything else last --
    so a later cap (`MAX_SYMBOL_ROWS`) drops the least-relevant rows instead of
    whichever ast.walk happened to visit last."""
    bare_candidates = {c for c in candidates if _BARE_IDENTIFIER_RE.match(c)}
    grep_hit_lines: dict[str, set[int]] = {}
    for term in list(bare_candidates)[:MAX_GREP_CANDIDATES]:
        for path, lineno in _grep_word_lines(repo, term, ref):
            grep_hit_lines.setdefault(path, set()).add(lineno)

    ranked: list[tuple[int, dict]] = []
    for rel in files[:MAX_SYMBOL_FILES]:
        if not rel.endswith(".py"):
            continue
        text = _read_file_at(repo, rel, ref)
        if text is None:
            continue
        defs = _ast_symbols(text, rel)
        is_hub = (text.count("\n") + 1) >= hub_threshold
        for d in defs:
            rank = _symbol_rank(d["name"], d["line_start"], d["line_end"], rel,
                                 bare_candidates, grep_hit_lines, path_line_hints)
            if is_hub and rank == 3:
                continue
            ranked.append((rank, d))
    ranked.sort(key=lambda item: item[0])
    return [d for _, d in ranked]


def _cache_path(home: Path, key: str) -> Path:
    return home / "derived" / "locate" / f"{store.validate_key(key)}.json"


def _head_sha(repo: Path) -> str:
    out = _run_git(repo, ["rev-parse", "HEAD"])
    return out[0] if out else ""


def compute(cfg: Config, key: str, *, force: bool = False) -> dict:
    """Idempotently (re)compute *key*'s locate hints, caching under
    ``derived/locate/<KEY>.json`` keyed by (spec_hash, HEAD) -- a second call
    for the same tree state reuses the cache and never re-shells to git
    (T-124 AC3). ``force=True`` (the ``maestro locate <KEY>`` verb) always
    recomputes.
    """
    from .dispatcher import _worker_cwd  # lazy: mirrors ops.py's own call sites
    home = cfg.home
    spec_hash = spec_hash_on_disk(home, key) or ""
    repo = _worker_cwd(cfg, key)
    head = _head_sha(repo)
    cache_file = _cache_path(home, key)
    if not force:
        cached = store.read_json(cache_file)
        if cached and cached.get("spec_hash") == spec_hash and cached.get("head") == head:
            return cached

    spec_path = store.spec_path(home, key)
    spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    candidates = extract_mentions(spec_text)
    path_line_hints = extract_path_line_mentions(spec_text)
    mention = resolve_mentions(repo, candidates)
    prior_edit = prior_edits(home, key)
    hints = merge_hints(mention, prior_edit, [])
    if len(hints) < MAX_HINTS:
        hints = merge_hints(mention, prior_edit, churn(repo))
    ranked = ranked_symbol_map(repo, [h["path"] for h in hints], candidates, path_line_hints)
    symbols, symbols_dropped = ranked[:MAX_SYMBOL_ROWS], max(0, len(ranked) - MAX_SYMBOL_ROWS)

    result = {
        "spec_hash": spec_hash,
        "head": head,
        "hints": hints,
        "symbols": symbols,
        "symbols_dropped": symbols_dropped,
        "prior_edits": prior_edit[:MAX_HINTS],
    }
    store.write_json(cache_file, result)
    return result


def load_cached(home: Path, key: str) -> dict | None:
    """Read-only: the last-computed locate result, or None if never computed.
    Never shells to git -- the read side of the (spec_hash, HEAD) cache."""
    return store.read_json(_cache_path(home, key))


# ---------------------------------------------------------------------------
# `maestro locate --eval`: a read-only replay over merged tickets proving the
# ranker's recall against the actual merged-commit oracle before `file_hints`
# is turned on for real (T-124 AC4/AC5).
# ---------------------------------------------------------------------------

_MERGED_TITLE_RE = re.compile(r"^([A-Za-z]+-\d+):\s")


def _merged_commits(repo: Path, home: Path, base: str) -> list[tuple[str, str, str]]:
    """(commit_sha, parent_sha, key) for every *base* commit titled '<KEY>:
    ...' where KEY currently has a spec on the board."""
    log = _run_git(repo, ["log", base, "--format=%H\t%P\t%s"])
    out: list[tuple[str, str, str]] = []
    for line in log:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        sha, parents, subject = parts
        parent = parents.split(" ")[0] if parents else ""
        if not parent:
            continue
        m = _MERGED_TITLE_RE.match(subject)
        if not m:
            continue
        key = m.group(1)
        if not store.spec_path(home, key).exists():
            continue
        out.append((sha, parent, key))
    return out


def _changed_files(repo: Path, parent: str, sha: str) -> set[str]:
    return set(_run_git(repo, ["diff", "--no-renames", "--name-only", parent, sha]))


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))?\s\+\d+(?:,\d+)?\s@@")


def _diff_old_ranges(repo: Path, parent: str, sha: str) -> dict[str, list[tuple[int, int]]]:
    """path -> old-side (line_start, line_end) ranges touched by each hunk of
    `git diff -U0 <parent> <sha>`. A 0-old-line hunk (a pure insertion) is
    reported as a single anchor line -- old_start, per `git diff`'s own
    convention for where the insertion lands (T-142's "symbol truth" recipe)."""
    lines = _run_git(repo, ["diff", "-U0", "--no-renames", parent, sha])
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith("+++ "):
            p = line[4:]
            current = p[2:] if p.startswith("b/") else None
            continue
        m = _HUNK_RE.match(line)
        if m and current:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            end = old_start if old_count == 0 else old_start + old_count - 1
            ranges.setdefault(current, []).append((old_start, end))
    return ranges


def _symbol_truth(repo: Path, parent: str, sha: str, parent_files: set[str]) -> set[str]:
    """`path::Qualname` for the innermost def/class enclosing each hunk's
    old-side lines, resolved against the PARENT commit's own AST -- never the
    working tree, so a later edit can't move the goalposts. A module-level
    hunk (an import, a constant -- no enclosing def) intentionally contributes
    no truth: T-142's Notes call this out as a choice to document, and treating
    a module-level edit as "the whole module" truth would make every stage's
    file-level recall trivially perfect on it, which isn't a meaningful signal
    for a SYMBOL-level ranker."""
    ranges_by_path = _diff_old_ranges(repo, parent, sha)
    truth: set[str] = set()
    defs_cache: dict[str, list[dict]] = {}
    for path, ranges in ranges_by_path.items():
        if not path.endswith(".py") or path not in parent_files:
            continue
        if path not in defs_cache:
            text = _read_file_at(repo, path, parent)
            defs_cache[path] = _ast_symbols(text, path) if text is not None else []
        defs = defs_cache[path]
        for start, end in ranges:
            best = None
            for d in defs:
                if d["line_start"] <= start and end <= d["line_end"]:
                    span = d["line_end"] - d["line_start"]
                    if best is None or span < best["line_end"] - best["line_start"]:
                        best = d
            if best is not None:
                truth.add(f"{path}::{best['qualname']}")
    return truth


def _recall_at(ranked: list[str], truth: set[str], n: int) -> float:
    if not truth:
        return 0.0
    return len(set(ranked[:n]) & truth) / len(truth)


def _mrr(ranked: list[str], truth: set[str]) -> float:
    for i, f in enumerate(ranked, start=1):
        if f in truth:
            return 1.0 / i
    return 0.0


def _score(rankings: dict[str, list[str]], truth: set[str]) -> dict[str, dict[str, list]]:
    scored = {}
    for stage, ranked in rankings.items():
        scored[stage] = {
            "recall_at_5": _recall_at(ranked, truth, 5),
            "recall_at_10": _recall_at(ranked, truth, 10),
            "mrr": _mrr(ranked, truth),
        }
    return scored


def _avg(rows: list[dict], field: str) -> float:
    if not rows:
        return 0.0
    return sum(r[field] for r in rows) / len(rows)


def run_eval(cfg: Config, home: Path, key: str | None, *, n: int | None = None) -> dict:
    """Replay every merged `<KEY>: ...` commit on *key*'s bound repo (or the
    board-wide default binding if *key* is None): rank the files that existed
    at the PARENT commit -- content taken from the parent, never the working
    tree -- per stage, and score recall@5, recall@10 and MRR against that
    commit's changed files that ALSO existed at the parent (a file the commit
    only added can never be ranked, so it's excluded from truth and reported
    under `excluded_added_files`). A commit with no such pre-existing changed
    file is still listed in `per_commit` (`scored: false`) but left out of the
    `stages` averages. Read-only; never mutates the board.
    """
    from . import repos as repos_mod
    binding = repos_mod.resolve(cfg, home, key) if key else repos_mod.implicit_default(cfg)
    if not binding.path:
        raise store.MaestroError("locate --eval: no repo path resolved to replay against")
    repo = Path(binding.path)
    commits = _merged_commits(repo, home, binding.base_branch)
    if n is not None:
        commits = commits[:n]

    per_commit: list[dict] = []
    scored_rows: list[dict] = []
    symbol_scored_rows: list[dict] = []
    excluded_added_files_count = 0
    for sha, parent, ticket_key in commits:
        spec_path = store.spec_path(home, ticket_key)
        spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
        candidates = extract_mentions(spec_text)
        path_line_hints = extract_path_line_mentions(spec_text)
        mention = resolve_mentions(repo, candidates, ref=parent)
        prior_edit = prior_edits(home, ticket_key)
        churn_files = churn(repo, ref=parent)
        merged = merge_hints(mention, prior_edit, churn_files)
        changed = _changed_files(repo, parent, sha)
        parent_files = set(_list_files(repo, parent))
        truth = changed & parent_files
        excluded_added = sorted(changed - parent_files)
        excluded_added_files_count += len(excluded_added)
        rankings = {
            "mention": mention,
            "prior_edit": prior_edit,
            "churn": churn_files,
            "merged": [h["path"] for h in merged],
        }
        merged_paths = [h["path"] for h in merged]
        symbol_truth = _symbol_truth(repo, parent, sha, parent_files)
        symbol_new = ranked_symbol_map(repo, merged_paths, candidates, path_line_hints, ref=parent)
        symbol_baseline = symbol_map(repo, merged_paths, ref=parent)
        symbol_rankings = {
            "symbol_new": [f"{r['path']}::{r['qualname']}" for r in symbol_new],
            "symbol_baseline": [f"{r['path']}::{r['name']}" for r in symbol_baseline],
        }
        row = {
            "key": ticket_key,
            "sha": sha,
            "scores": _score(rankings, truth),
            "symbol_scores": _score(symbol_rankings, symbol_truth),
            "excluded_added_files": excluded_added,
            "scored": bool(truth),
            "symbol_scored": bool(symbol_truth),
        }
        per_commit.append(row)
        if truth:
            scored_rows.append(row)
        if symbol_truth:
            symbol_scored_rows.append(row)

    stages = ("mention", "prior_edit", "churn", "merged")
    summary = {
        stage: {
            "recall_at_5": _avg([c["scores"][stage] for c in scored_rows], "recall_at_5"),
            "recall_at_10": _avg([c["scores"][stage] for c in scored_rows], "recall_at_10"),
            "mrr": _avg([c["scores"][stage] for c in scored_rows], "mrr"),
        }
        for stage in stages
    }
    for stage in ("symbol_new", "symbol_baseline"):
        summary[stage] = {
            "recall_at_5": _avg([c["symbol_scores"][stage] for c in symbol_scored_rows], "recall_at_5"),
            "recall_at_10": _avg([c["symbol_scores"][stage] for c in symbol_scored_rows], "recall_at_10"),
            "mrr": _avg([c["symbol_scores"][stage] for c in symbol_scored_rows], "mrr"),
        }
    return {
        "commits_evaluated": len(per_commit),
        "commits_scored": len(scored_rows),
        "commits_excluded_no_prior_changes": len(per_commit) - len(scored_rows),
        "commits_scored_symbols": len(symbol_scored_rows),
        "commits_excluded_no_symbol_truth": len(per_commit) - len(symbol_scored_rows),
        "excluded_added_files_count": excluded_added_files_count,
        "baseline_mention_recall_at_5": summary["mention"]["recall_at_5"],
        "stages": summary,
        "per_commit": per_commit,
    }
