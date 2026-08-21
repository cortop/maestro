import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maestro import event_log, skills_install, snapshot as snap_mod, store  # noqa: E402
from maestro.config import Config  # noqa: E402
from fault_injection import FaultInjector  # noqa: E402


def git(*args, cwd):
    """Run a real git command (never mocked) -- shared by worktree/preflight/
    multi-repo dispatcher tests."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_origin_and_repo(tmp_path, name="repo", base_branch="main"):
    """A bare origin plus a clone (standing in for a project repo maestro builds
    in). Real git underneath, never mocked. `name`/`base_branch` let callers
    build N independent repos under one `tmp_path` (multi-repo dispatcher
    tests, MR-3+) -- the single-repo default matches the original
    origin.git/repo shape."""
    origin = tmp_path / f"{name}-origin.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", base_branch, cwd=origin)

    repo = tmp_path / name
    git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text(f"{name}\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    git("push", "-q", "-u", "origin", base_branch, cwd=repo)
    return origin, repo


@pytest.fixture
def home(tmp_path):
    for d in ("events", "inbox", "tickets", "worktrees", "derived/snapshots", "derived/cursors"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    # T-99: `store.board_state`'s CORE fingerprint also requires `config.toml`
    # -- write a bare one so every existing test's `home` still classifies
    # "ok" (this fixture stands in for a real, live board everywhere else in
    # the suite; only the T-99 tests themselves construct a deliberately
    # broken home).
    config_path = tmp_path / "config.toml"
    if not config_path.exists():
        config_path.write_text("[maestro]\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg(home):
    return Config(home=home, max_concurrency=3, backoff_base=10, max_failures=3)


@pytest.fixture
def faults(monkeypatch):
    """A fresh `FaultInjector` (RB-11's crash-injection shim, see
    `tests/fault_injection.py`) for the test -- patches auto-reverted at
    teardown via the standard `monkeypatch` fixture."""
    return FaultInjector(monkeypatch)


def assert_user_scope_confined(tmp_path):
    """QW-4 fail-closed guard: every resolver in
    `skills_install.USER_SCOPE_RESOLVERS`, called exactly as production code
    would call it (no `cfg` override), must resolve under *tmp_path* --
    raises loudly the instant one doesn't, rather than letting a forgotten
    redirect quietly write to the developer's real $HOME. Factored out of the
    autouse fixture below so `test_user_scope_guard.py` can call it directly
    against a deliberately escaping resolver and assert the raise (AC3) --
    not just assert intent.
    """
    tmp_path = Path(tmp_path).resolve()
    for resolver in skills_install.USER_SCOPE_RESOLVERS:
        resolved = Path(resolver()).resolve()
        try:
            resolved.relative_to(tmp_path)
        except ValueError:
            raise AssertionError(
                f"skills_install.{resolver.__name__}() resolved to {resolved}, "
                f"outside this test's tmp_path ({tmp_path}) -- a user-scope "
                "install would have written to the developer's real home. "
                "See skills_install.USER_SCOPE_RESOLVERS."
            ) from None


@pytest.fixture(autouse=True)
def _guard_user_scope_installs(tmp_path, monkeypatch):
    """QW-4: autouse so no individual test has to remember to redirect every
    user-scope install target -- points every resolver in
    `skills_install.USER_SCOPE_RESOLVERS` at this test's own `tmp_path` via
    their env-var overrides (`MAESTRO_USER_COMMANDS_DIR` /
    `MAESTRO_OPENCODE_COMMANDS_DIR` -- `opencode_user_agent_dir` and
    `opencode_user_config_path` both derive from the latter, so redirecting
    the two env vars covers all four resolvers), then fails closed if any of
    them didn't actually land there. A test that still does its own
    `monkeypatch.setenv` of these vars (several already do) just overrides to
    a *different* tmp_path-scoped location -- env var already wins over this
    fixture in `skills_install`'s own precedence order, and the assertion
    below still passes either way.
    """
    monkeypatch.setenv("MAESTRO_USER_COMMANDS_DIR", str(tmp_path / "guard-claude-user-commands"))
    monkeypatch.setenv("MAESTRO_OPENCODE_COMMANDS_DIR",
                       str(tmp_path / "guard-opencode-user-commands"))
    assert_user_scope_confined(tmp_path)
    yield


def seed_ticket(home, key, title, *, phase=None, questions=None, pr=None, tier=1):
    """Append events for one ticket and fold its snapshot, mimicking the real flow.

    Drives state only through event_log + the fold, exactly as a reconciler would,
    so the resulting home is indistinguishable from one a live run produced.
    """
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: {tier}\n\n## Intent\n{title}\n\n"
                       "## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": title, "source": "test", "spec_hash": "x"}, actor="d")
    if pr is not None:
        event_log.append(home, key, "PrOpened",
                         {"number": pr, "url": f"https://github.com/cortop/maestro/pull/{pr}",
                          "draft": True}, actor="r")
    for qid, text in (questions or {}).items():
        event_log.append(home, key, "QuestionAsked", {"qid": qid, "text": text}, actor="d")
    if phase is not None:
        event_log.append(home, key, "PhaseChanged", {"phase": phase, "reason": ""}, actor="r")
    return snap_mod.rebuild(home, key)


@pytest.fixture
def seeded_home(home):
    """A home with tickets spanning the phases that historically broke the TUI."""
    seed_ticket(home, "T-1", "awaiting human with questions",
                phase="awaiting-human",
                questions={"q1": "Shall I proceed [now]?", "q2": "Use [a] or [b]?"})
    seed_ticket(home, "T-2", "stalled/degraded ticket", phase="degraded")
    seed_ticket(home, "T-3", "implementing with PR", phase="implementing", pr=15)
    # awaiting-ci historically crashed the detail pane (unquoted [link=URL] markup)
    seed_ticket(home, "T-4", "awaiting ci with PR", phase="awaiting-ci", pr=16)
    seed_ticket(home, "T-5", "ready ticket", phase="ready")
    return home
