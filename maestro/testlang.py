"""Per-language added/deleted test-name extraction and selector formatting --
the single source of truth `ops.run_ac_checks` (a `test:` AC annotation) and
`dispatcher`'s H4 test-deletion gate both select from, keyed off a repo
binding's `language` (`repos.RepoBinding.language`).

T-84: before this module, `ops.py` carried one module-level pytest regex as
the sole added-test extractor/selector, and `dispatcher.py`'s H4 gate carried
an independent second copy, hard-scoped to `tests/`. Both are now table
lookups against `resolve()` below. `python` is the table's default entry --
an unconfigured `RepoBinding.language` (``None``) resolves to it, keeping
every existing regex/selector/pathspec byte-identical to before this module
existed (the spec's ships-dark requirement). A `language` set to anything
NOT in `SUPPORTED` is a configuration error the caller must surface legibly
(`UnsupportedLanguage`) rather than let a `test:` check fail closed forever.

T-98: `language` is the EXTRACTION axis only -- where a test name lives in a
diff, and (via `test_file_globs`) which files count as "a test file" for H4.
A `LanguageProfile.format_selector` conflated that with a second, orthogonal
axis -- how to INVOKE one named test -- and every one of the three formatters
below is `test_command`-prefix + appended args, so a repo that builds through
something else (Bazel wrapping Go/TypeScript, say) can never compose a valid
command even though its `language` extraction is perfectly correct.
`[repos.<name>] test_selector` (see `render_selector`/`selector_for` below)
is that second axis: an optional, per-repo format-string invocation template
that overrides a profile's `format_selector` without touching extraction at
all -- "extract like Go, invoke like Bazel".
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

SUPPORTED = ("python", "go", "typescript")


@dataclass(frozen=True)
class LanguageProfile:
    name: str
    # Matches an ADDED (`+...`) line in a unified diff, capturing the test
    # name in group(1). What `_diff_added_test_names` scans a `test:`
    # annotation's own path-scoped diff with.
    added_re: re.Pattern
    # Matches an added OR deleted (`^([+-])...`) line -- group(1) is the
    # sign, group(2) the test name. Powers the H4 net-deletion diff.
    line_re: re.Pattern
    # git pathspec(s) scoping which files count as "a test file" for the H4
    # net-deletion scan. `python` keeps the historical `tests/`-only scope
    # byte-identically; `go`/`typescript` tests are co-located with source,
    # so they scope by filename suffix/directory instead of a fixed path.
    test_file_globs: tuple[str, ...]
    # (test_command, path, sorted test names) -> the full shell command to
    # run for a `test:` annotation's check.
    format_selector: Callable[[str, str, Sequence[str]], str]


def _python_selector(test_command: str, path: str, names: Sequence[str]) -> str:
    selector = " ".join(f"{path}::{n}" for n in names)
    return f"{test_command} {selector}"


def _go_pkg_dir(path: str) -> str:
    d = path.rsplit("/", 1)[0] if "/" in path else ""
    return f"./{d}" if d else "."


def _go_selector(test_command: str, path: str, names: Sequence[str]) -> str:
    pattern = "|".join(names)
    return f"{test_command} {_go_pkg_dir(path)} -run '^({pattern})$'"


def _typescript_selector(test_command: str, path: str, names: Sequence[str]) -> str:
    pattern = "|".join(re.escape(n) for n in names)
    return f'{test_command} {path} -t "{pattern}"'


# T-79's original, sole extractor/selector -- unchanged byte-for-byte so an
# unconfigured (or Python) repo binding behaves exactly as it did before this
# module existed.
PYTHON = LanguageProfile(
    name="python",
    added_re=re.compile(r"^\+\s*def\s+(test_\w+)\s*\("),
    line_re=re.compile(r"^([+-])\s*def\s+(test_\w+)\s*\("),
    test_file_globs=("tests/",),
    format_selector=_python_selector,
)

# `func TestWidget(t *testing.T)` -- Go's exported-test-function convention;
# co-located `*_test.go` beside its source, never a fixed `tests/` directory.
GO = LanguageProfile(
    name="go",
    added_re=re.compile(r"^\+\s*func\s+(Test\w+)\s*\("),
    line_re=re.compile(r"^([+-])\s*func\s+(Test\w+)\s*\("),
    test_file_globs=("*_test.go",),
    format_selector=_go_selector,
)

# `it("...")` / `test("...")` -- jest/mocha/vitest's convention; files are
# `*.spec.ts(x)` / `*.test.ts(x)`, often under `__tests__/`, never a fixed
# `tests/` directory.
TYPESCRIPT = LanguageProfile(
    name="typescript",
    added_re=re.compile(r"^\+\s*(?:it|test)\s*\(\s*['\"`](.+?)['\"`]"),
    line_re=re.compile(r"^([+-])\s*(?:it|test)\s*\(\s*['\"`](.+?)['\"`]"),
    test_file_globs=("*.spec.ts", "*.test.ts", "*.spec.tsx", "*.test.tsx", "__tests__/"),
    format_selector=_typescript_selector,
)

_TABLE: dict[str, LanguageProfile] = {p.name: p for p in (PYTHON, GO, TYPESCRIPT)}


class UnsupportedLanguage(ValueError):
    """*language* is set but isn't a key `resolve()` recognizes -- callers
    must surface this as a legible, one-time error (T-84: `ops.fail(...,
    dead_letter=True)`), never let a `test:` check silently fail closed
    forever."""


def resolve(language: str | None) -> LanguageProfile:
    """*language* is a repo binding's resolved `RepoBinding.language`: ``None``
    (or an empty string -- unconfigured) resolves to `PYTHON`, today's sole
    behavior, kept byte-identical (T-84 ships-dark requirement). Any other
    value must be one of `SUPPORTED` or this raises `UnsupportedLanguage`."""
    if not language:
        return PYTHON
    profile = _TABLE.get(language)
    if profile is None:
        raise UnsupportedLanguage(
            f"unsupported language {language!r} -- supported: {', '.join(SUPPORTED)}")
    return profile


# ---------------------------------------------------------------------------
# T-98: `[repos.<name>] test_selector` -- an optional, per-repo invocation
# template, orthogonal to `language`'s extraction axis. A closed placeholder
# set over (test_command, path, names) -- the exact three values every
# built-in `format_selector` above already composes from.
# ---------------------------------------------------------------------------

SELECTOR_PLACEHOLDERS = frozenset({"test_command", "path", "dir", "name", "names"})

# go/python test names are `\w+` by construction (their own `added_re`s only
# ever capture identifier characters) and always pass; a free-text TypeScript
# `it("...")` name is the real injection surface into `_run_shell`'s
# `shell=True` command line. Permit the charset a reasonable test name needs
# (word chars, path/namespace separators, spaces) and refuse everything else
# outright -- "reject" rather than guess at the surrounding template's own
# quoting convention (which this module has no visibility into).
_UNSAFE_NAME_RE = re.compile(r"[^\w./: -]")


class UnsafeTestName(ValueError):
    """A test name contains a character unsafe to interpolate into a
    `test_selector` template's `shell=True` command line (T-98). Callers must
    turn this into a check failure -- never run the composed command anyway."""


def validate_selector_template(template: str) -> None:
    """Raise ``ValueError`` if *template* is malformed (unbalanced/misplaced
    braces) or references a placeholder outside `SELECTOR_PLACEHOLDERS` --
    called at `config.load()` (T-98) so a typo'd `[repos.<name>]
    test_selector` fails closed at load time, exactly like an unrecognized
    `language`, never a `test:` check that silently fails closed forever."""
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template)
                 if name is not None]
    except ValueError as exc:
        raise ValueError(f"test_selector {template!r} is malformed: {exc}") from exc
    unknown = sorted(set(fields) - SELECTOR_PLACEHOLDERS)
    if unknown:
        raise ValueError(
            f"test_selector {template!r} has unrecognized placeholder(s): "
            f"{', '.join(unknown)} -- supported: {', '.join(sorted(SELECTOR_PLACEHOLDERS))}")


def _quote_name(name: str) -> str:
    if _UNSAFE_NAME_RE.search(name):
        raise UnsafeTestName(name)
    return name


def _selector_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def render_selector(template: str, test_command: str, path: str, names: Sequence[str]) -> str:
    """Render a `[repos.<name>] test_selector` *template* against one
    `test:` annotation's already-resolved ``(test_command, path, names)`` --
    the per-repo invocation-axis counterpart to a `LanguageProfile`'s own
    `format_selector`, same call shape. Raises `UnsafeTestName` (never
    interpolates it raw) if any name in *names* contains a character unsafe
    for `_run_shell`'s `shell=True` command line."""
    safe_names = [_quote_name(n) for n in names]
    return template.format(
        test_command=test_command,
        path=path,
        dir=_selector_dir(path),
        name=safe_names[0] if safe_names else "",
        names="|".join(safe_names),
    )


def selector_for(profile: LanguageProfile,
                 test_selector: str | None) -> Callable[[str, str, Sequence[str]], str]:
    """The selector-formatting callable `ops._run_named_test` should compose
    a `test:` command through: *profile*'s own `format_selector` when
    *test_selector* is unset (byte-identical to before this function
    existed), else *test_selector* rendered via `render_selector` -- the
    invocation axis overriding the language profile's built-in one without
    touching extraction (`added_re`/`line_re`/`test_file_globs`) at all."""
    if not test_selector:
        return profile.format_selector
    return lambda test_command, path, names: render_selector(
        test_selector, test_command, path, names)


# T-96: an UNSET `language` silently resolves to PYTHON above (T-84's
# deliberate ships-dark default) -- but that default is simply wrong when the
# repo's actual test surface is Go or TypeScript, and nothing catches it: a
# `test:` annotation's presence check runs the PYTHON regex against a
# `*_test.go` diff, never matches, and reports "not added by this diff"
# forever, even though the suite is green. The two guess functions below are
# the single shared oracle every T-96 caller (`ops.run_ac_checks`'s presence
# gate, the H4 deletion scan, `maestro doctor`) reads from, so "language
# unset + non-python surface" is detected identically everywhere instead of
# three divergent heuristics.

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def guess_from_path(path: str) -> str | None:
    """A `test:` annotation's own path extension, when it unambiguously names
    a non-python test surface (.go / .ts / .tsx). None for .py or any other
    extension -- inconclusive, never a false positive."""
    for ext, lang in _EXTENSION_LANGUAGE.items():
        if path.endswith(ext):
            return lang
    return None


def guess_from_repo_root(repo_root: Path) -> str | None:
    """A repo root's own marker files, used when no annotation path is
    available (the H4 scan, `maestro doctor`): a `go.mod` or `package.json`
    at the root names Go/TypeScript. None (inconclusive) never overrides the
    python default on its own -- this must never false-positive on a real
    python repo that just organizes its tests differently."""
    if (repo_root / "go.mod").is_file():
        return "go"
    if (repo_root / "package.json").is_file():
        return "typescript"
    return None


class MismatchedLanguage(ValueError):
    """*language* is unset (silently defaults to PYTHON, T-84's ships-dark
    rule) but the actual test surface being checked -- an annotation's path
    extension, or the repo root's own marker files -- contradicts that
    default (e.g. `test_command` set on a Go repo with no `[repos.<name>]
    language`). Callers must surface this the same way as
    `UnsupportedLanguage` (T-96: one legible, one-time dead-letter naming
    `language` and the repo table to set) rather than let a `test:` check run
    the wrong regex and fail closed forever."""


def resolve_strict(language: str | None, *, ann_path: str | None = None,
                   repo_root: Path | None = None) -> LanguageProfile:
    """Like `resolve()`, but when *language* is unset (would default to
    PYTHON) AND a guess from *ann_path* (a `test:` annotation's own path) or
    *repo_root* (marker files, checked only when *ann_path* gave no verdict)
    names a different language, raises `MismatchedLanguage` instead of
    silently defaulting -- the T-96 fail-closed gate. An explicitly SET
    *language* is unaffected here; a bad explicit value is still `resolve()`'s
    own `UnsupportedLanguage`."""
    if not language:
        guessed = guess_from_path(ann_path) if ann_path else None
        if guessed is None and repo_root is not None:
            guessed = guess_from_repo_root(repo_root)
        if guessed and guessed != "python":
            raise MismatchedLanguage(
                f"language is unset (defaults to python) but this repo's test "
                f"surface looks like {guessed!r} -- set language = {guessed!r} "
                f"in [repos.<name>] (or board-wide [maestro] language) to fix")
    return resolve(language)
