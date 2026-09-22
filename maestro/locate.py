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
MAX_CHURN_COMMITS = 200
MAX_GREP_CANDIDATES = 25

_GIT_TIMEOUT = 20

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATHLIKE_RE = re.compile(r"\b[\w][\w./-]*\.[A-Za-z][\w]{0,8}\b")
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def _list_files(repo: Path, ref: str | None) -> list[str]:
    if ref:
        return _run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    return _run_git(repo, ["ls-files"])


def _grep_word(repo: Path, term: str, ref: str | None) -> list[str]:
    if ref:
        return _run_git(repo, ["grep", "-lw", "-I", term, ref, "--"])
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


def symbol_map(repo: Path, files: list[str]) -> list[dict]:
    """One `ast` walk per top Python file -- def/class name -> path, line
    range, first docstring line -- so a session can `Read` with offset/limit
    instead of re-reading the whole file. Non-.py files, and any file that
    fails to parse, are silently skipped (fail-open -- a symbol map is a
    convenience, never a requirement)."""
    symbols: list[dict] = []
    for rel in files[:MAX_SYMBOL_FILES]:
        if not rel.endswith(".py"):
            continue
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
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
    mention = resolve_mentions(repo, candidates)
    prior_edit = prior_edits(home, key)
    hints = merge_hints(mention, prior_edit, [])
    if len(hints) < MAX_HINTS:
        hints = merge_hints(mention, prior_edit, churn(repo))
    symbols = symbol_map(repo, [h["path"] for h in hints])

    result = {
        "spec_hash": spec_hash,
        "head": head,
        "hints": hints,
        "symbols": symbols,
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
    return set(_run_git(repo, ["diff", "--name-only", parent, sha]))


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
    commit's actual changed files. Read-only; never mutates the board.
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
    for sha, parent, ticket_key in commits:
        spec_path = store.spec_path(home, ticket_key)
        spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
        candidates = extract_mentions(spec_text)
        mention = resolve_mentions(repo, candidates, ref=parent)
        prior_edit = prior_edits(home, ticket_key)
        churn_files = churn(repo, ref=parent)
        merged = merge_hints(mention, prior_edit, churn_files)
        truth = _changed_files(repo, parent, sha)
        rankings = {
            "mention": mention,
            "prior_edit": prior_edit,
            "churn": churn_files,
            "merged": [h["path"] for h in merged],
        }
        per_commit.append({"key": ticket_key, "sha": sha, "scores": _score(rankings, truth)})

    stages = ("mention", "prior_edit", "churn", "merged")
    summary = {
        stage: {
            "recall_at_5": _avg([c["scores"][stage] for c in per_commit], "recall_at_5"),
            "recall_at_10": _avg([c["scores"][stage] for c in per_commit], "recall_at_10"),
            "mrr": _avg([c["scores"][stage] for c in per_commit], "mrr"),
        }
        for stage in stages
    }
    return {
        "commits_evaluated": len(per_commit),
        "baseline_mention_recall_at_5": summary["mention"]["recall_at_5"],
        "stages": summary,
        "per_commit": per_commit,
    }
