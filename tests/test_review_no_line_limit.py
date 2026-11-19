"""T-285: a review round never evaluates the `pr_split_threshold` diff-size limit.

Real git, real CLI and real dispatcher sweeps; only the code-host read and the
`claude -p` spawn are faked.
"""
from conftest import git, make_origin_and_repo
from test_review_kind import KEY, FakeVCS, _review
from maestro import config, dispatcher as disp, event_log, snapshot as snap_mod
from maestro.sessions import DryRunSessions


def _push_big_pr(origin, repo, name="big", number=7, lines=1000):
    import subprocess
    git("checkout", "-q", "-b", name, cwd=repo)
    (repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(lines)))
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()
    git("push", "-q", "-f", "origin", f"HEAD:refs/pull/{number}/head", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    return sha


def test_review_over_800_lines_proceeds_without_size_question(home, tmp_path, monkeypatch):
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        f'[maestro]\n[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    sha = _push_big_pr(origin, repo)
    assert _review(home, FakeVCS(sha), monkeypatch=monkeypatch) == 0

    cfg = config.load(str(home))
    assert cfg.pr_split_threshold == 800
    sessions = DryRunSessions()
    for i in range(5):
        disp.dispatch(cfg, sessions, now=1000 + i * 1000)
        if snap_mod.load(home, KEY).phase == "qa":
            break

    snap = snap_mod.load(home, KEY)
    assert snap.phase == "qa"
    assert not snap.open_questions
    assert not any(q.startswith("review-size") for q in snap.answered_questions)
    assert not any("pr_split_threshold" in str(e) or "review-size" in str(e)
                   for e in event_log.read(home, KEY))
