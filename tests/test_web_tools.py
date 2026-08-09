"""L-7/GA-3: spawned reconciler sessions must get the maestro CLI agent verbs
(never the bare wildcard) and WebSearch/WebFetch via --allowedTools."""
import argparse
import os
from unittest.mock import MagicMock, patch

from maestro import cli, event_log, inbox, snapshot as snap_mod, store
from maestro.cli import _AGENT_TOOL_VERBS
from maestro.config import load
from maestro.dispatcher import tier_denylist
from maestro.statemachine import Phase

_HUMAN_ONLY_VERBS = ("approve", "ans", "answer", "restore", "fleet", "init",
                     "dispatch", "prune-logs", "cmd", "tui", "compact", "archive-done")


def _seed_ready(home, key="T-1", tier=0):
    store.atomic_write(store.spec_path(home, key), f"# {key}\napproval_tier: {tier}\n")
    event_log.append(home, key, "TicketCreated", {"title": key, "spec_hash": "x"}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, key)


def _capture_popen_cmds():
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    captured = []

    def capture_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return fake_proc

    return captured, capture_popen


def test_config_reconcile_web_tools_defaults_true(home):
    cfg = load(str(home))
    assert cfg.reconcile_web_tools is True


def test_config_reconcile_web_tools_can_be_disabled(home):
    (home / "config.toml").write_text("[maestro]\nreconcile_web_tools = false\n")
    cfg = load(str(home))
    assert cfg.reconcile_web_tools is False


# --- GA-10: reconcile_allowed_tools (board-wide + per-repo) --------------------------

def test_config_reconcile_allowed_tools_defaults_empty(home):
    cfg = load(str(home))
    assert cfg.reconcile_allowed_tools == []


def test_config_reconcile_allowed_tools_parses_board_wide_list(home):
    (home / "config.toml").write_text(
        '[maestro]\nreconcile_allowed_tools = ["Bash(npm test:*)"]\n')
    cfg = load(str(home))
    assert cfg.reconcile_allowed_tools == ["Bash(npm test:*)"]


def test_config_repos_table_parses_reconcile_allowed_tools(home):
    (home / "config.toml").write_text(
        '[maestro]\nrepo_path = "/repo/default"\n\n'
        '[repos.alpha]\npath = "/repo/alpha"\n'
        'reconcile_allowed_tools = ["Bash(cargo test:*)"]\n\n'
        '[repos.beta]\npath = "/repo/beta"\n')
    cfg = load(str(home))
    assert cfg.repos["alpha"]["reconcile_allowed_tools"] == ["Bash(cargo test:*)"]
    # Unset inherits the board-wide list (it's unioned in at spawn time, not replaced) --
    # an absent [repos.*] key means "nothing extra", not "no tools at all".
    assert cfg.repos["beta"]["reconcile_allowed_tools"] == []


def _expected_grant(*, web_tools: bool) -> str:
    rules = [f"Bash(maestro {verb}:*)" for verb in _AGENT_TOOL_VERBS]
    if web_tools:
        rules += ["WebSearch", "WebFetch"]
    return ",".join(rules)


def test_dispatch_spawn_command_includes_web_search_and_fetch(home):
    """Real `maestro dispatch` (not dry-run) must grant the maestro verbs plus WebSearch/WebFetch."""
    _seed_ready(home)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == _expected_grant(web_tools=True)


def test_dispatch_grants_maestro_verbs_without_web_tools_when_disabled(home):
    """reconcile_web_tools=false must still grant the maestro CLI verbs -- only
    WebSearch/WebFetch drop out (GA-3: previously --allowedTools was omitted
    entirely, disarming the reconciler's own bookkeeping)."""
    (home / "config.toml").write_text("[maestro]\nreconcile_web_tools = false\n")
    _seed_ready(home)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    value = cmd[idx + 1]
    assert value == _expected_grant(web_tools=False)
    assert "WebSearch" not in value
    assert "WebFetch" not in value
    for verb in _AGENT_TOOL_VERBS:
        assert f"Bash(maestro {verb}:*)" in value


def test_nudge_spawn_command_includes_web_search_and_fetch(home):
    """The in-process nudge path (triggered by `ans`) must also grant the tools."""
    store.atomic_write(store.spec_path(home, "T-1"), "# T-1\napproval_tier: 0\n")
    event_log.append(home, "T-1", "TicketCreated", {"title": "T-1", "spec_hash": "x"}, actor="d")
    event_log.append(home, "T-1", "PhaseChanged", {"phase": Phase.AWAITING_HUMAN.value}, actor="r")
    event_log.append(home, "T-1", "QuestionAsked", {"qid": "q1", "text": "ok?"}, actor="r")
    snap_mod.rebuild(home, "T-1")
    inbox.append_command(home, "T-1", "ans", {"text": "yes", "qid": "q1"})

    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "ans", "T-1", "yes"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == _expected_grant(web_tools=True)


def test_agent_tool_verbs_named_exactly():
    """AC: exactly one Bash(maestro <verb>:*) rule per [agent]-tagged verb plus
    env/show/create, and no rule for the human-only/irreversible verbs."""
    granted = set(_AGENT_TOOL_VERBS)
    expected = {
        "local-backup", "snapshot", "events", "append", "set-phase", "ask",
        "fold-inbox", "inbox-ack", "observe-spec", "requeue", "fail", "impl-turn",
        "verify-ac", "qa-brief", "qa-verdict", "finalize", "release",
        "check-conflicts", "check-merged", "fold-steps", "env", "show", "create",
    }
    assert granted == expected
    assert not (granted & set(_HUMAN_ONLY_VERBS))


def test_bare_wildcard_never_appears(home):
    """Explicit regression guard: the bare Bash(maestro:*) wildcard must never
    be the emitted rule (it would let a reconciler self-approve, AD-1)."""
    _seed_ready(home)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        cli.main(["--home", str(home), "dispatch"])
    cmd = captured[0]
    value = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash(maestro:*)" not in value.split(",")


def test_agent_grant_matches_cli_agent_tags():
    """Drift guard: every "[agent]"-tagged verb in build_parser() must appear
    in _AGENT_TOOL_VERBS, and vice versa (minus env/show/create, which are
    genuinely used by skills but not "[agent]"-tagged -- see the comment
    beside _AGENT_TOOL_VERBS in cli.py)."""
    parser = cli.build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    agent_tagged = {
        choice.dest for choice in subparsers_action._choices_actions
        if (choice.help or "").startswith("[agent]")
    }
    non_agent_tagged_extras = {"env", "show", "create"}
    assert agent_tagged == set(_AGENT_TOOL_VERBS) - non_agent_tagged_extras
    assert non_agent_tagged_extras <= set(_AGENT_TOOL_VERBS)


def test_dispatch_allowed_tools_composes_with_tier_denylist(home):
    """A tier-1 key's spawn argv must carry both --disallowedTools (deny) and
    --allowedTools (the maestro grant), with no overlap between them."""
    _seed_ready(home, tier=1)
    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    assert len(captured) == 1
    cmd = captured[0]
    assert "--disallowedTools" in cmd
    deny_value = cmd[cmd.index("--disallowedTools") + 1]
    assert deny_value == ",".join(tier_denylist(1))
    allow_value = cmd[cmd.index("--allowedTools") + 1]
    assert not (set(deny_value.split(",")) & set(allow_value.split(",")))


# --- GA-10: per-repo reconcile_allowed_tools reaches the spawn argv per key ----------

_TWO_REPO_TOML = """\
[repos.alpha]
path = "{alpha}"
reconcile_allowed_tools = ["Bash(cargo test:*)"]

[repos.beta]
path = "{beta}"
reconcile_allowed_tools = ["Bash(npm test:*)"]
"""


def _seed_ready_bound(home, key, repo_name, tier=0):
    store.atomic_write(store.spec_path(home, key),
                       f"# {key}\napproval_tier: {tier}\nrepo: {repo_name}\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": "x", "repo": repo_name}, actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": Phase.READY.value}, actor="r")
    snap_mod.rebuild(home, key)


def test_dispatch_per_repo_allowed_tools_reach_their_own_spawn_argv_only(home):
    """Real-surface QA over a temp MAESTRO_HOME (AD-4): two [repos.*] tables declaring
    different reconcile_allowed_tools, one ticket bound to each, driven through the real
    `maestro dispatch` with ONLY subprocess.Popen patched. Each key's captured argv must
    carry its own repo's tools (plus the board-wide GA-3 grant + WebSearch/WebFetch), not
    the other repo's, and exactly one --allowedTools flag -- never two."""
    alpha = home / "alpha-repo"
    beta = home / "beta-repo"
    alpha.mkdir()
    beta.mkdir()
    (home / "config.toml").write_text(
        _TWO_REPO_TOML.format(alpha=alpha, beta=beta), encoding="utf-8")

    _seed_ready_bound(home, "A-1", "alpha")
    _seed_ready_bound(home, "B-1", "beta")

    captured, capture_popen = _capture_popen_cmds()
    with patch("subprocess.Popen", side_effect=capture_popen):
        rc = cli.main(["--home", str(home), "dispatch"])
    assert rc == 0
    # alpha/beta aren't real git checkouts, so the dispatcher's own repo-preflight/sync
    # machinery also shells out via subprocess.Popen -- filter down to the two `claude`
    # reconciler spawns this test actually cares about.
    claude_cmds = [cmd for cmd in captured if cmd[0] == "claude"]
    assert len(claude_cmds) == 2

    by_key = {cmd[cmd.index("-n") + 1].removeprefix("reconcile-"): cmd for cmd in claude_cmds}
    assert set(by_key) == {"A-1", "B-1"}

    for key, other_tool in (("A-1", "Bash(npm test:*)"), ("B-1", "Bash(cargo test:*)")):
        cmd = by_key[key]
        assert cmd.count("--allowedTools") == 1, f"{key}: expected exactly one --allowedTools flag"
        value = cmd[cmd.index("--allowedTools") + 1].split(",")
        for verb in _AGENT_TOOL_VERBS:
            assert f"Bash(maestro {verb}:*)" in value
        assert "WebSearch" in value and "WebFetch" in value
        assert other_tool not in value, f"{key}'s argv leaked the other repo's tool"

    assert "Bash(cargo test:*)" in by_key["A-1"][by_key["A-1"].index("--allowedTools") + 1]
    assert "Bash(npm test:*)" in by_key["B-1"][by_key["B-1"].index("--allowedTools") + 1]
