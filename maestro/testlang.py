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
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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
