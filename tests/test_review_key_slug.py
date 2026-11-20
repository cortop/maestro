"""M-6: a review ticket minted from a PR is keyed `R-<number>-<title slug>`.

Real CLI and git; only the code-host read (`pr_head`) is faked.
"""
import pytest

from conftest import make_origin_and_repo
from maestro import cli, event_log, ops, snapshot as snap_mod, store
from maestro.config import Config
from test_review_kind import FakeVCS, _push_pr_head


@pytest.fixture
def board(home, tmp_path, monkeypatch):
    _SHAS.clear()
    origin, repo = make_origin_and_repo(tmp_path, name="widgets")
    (home / "config.toml").write_text(
        f'[maestro]\n[repos.widgets]\npath = "{repo}"\nslug = "acme/widgets"\n')
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return home, origin, repo


def _review(vcs, number, monkeypatch):
    from maestro import providers
    monkeypatch.setattr(providers, "get_vcs", lambda cfg: vcs)
    return cli.main(["review", f"https://github.com/acme/widgets/pull/{number}"])


_SHAS: dict = {}


def _mint(board, monkeypatch, number, title):
    home, origin, repo = board
    sha = _SHAS.get(number) or _push_pr_head(origin, repo, home.parent, f"pr-{number}", number=number)
    _SHAS[number] = sha
    vcs = FakeVCS(sha)
    vcs.title = title
    assert _review(vcs, number, monkeypatch) == 0
    return vcs


@pytest.mark.parametrize("title,slug", [
    ("Fix race in retry loop", "fix-race-in-retry-loop"),
    ("  Ünïcode: Fix   the *thing*!! ", "unicode-fix-the-thing"),
    ("word " * 20, "word-word-word-word-word-word-word-word"),  # 39 chars, word boundary
    ("", ""), ("!!! ???", ""), (None, ""),
])
def test_slug(title, slug):
    assert ops.review_title_slug(title) == slug
    assert len(slug) <= ops.REVIEW_SLUG_MAX


def test_minted_key_event_and_listings(board, monkeypatch, capsys):
    home, _, _ = board
    _mint(board, monkeypatch, 7, "Fix race in retry loop")
    key = "R-7-fix-race-in-retry-loop"
    ev = [e for e in event_log.read(home, key) if e["type"] == "TicketCreated"]
    assert len(ev) == 1
    snap = snap_mod.load(home, key)
    assert snap.kind == "review" and snap.external_id == "acme/widgets#7"
    for p in (home / "events" / f"{key}.jsonl", home / "tickets" / key / "spec.md",
              store.worktree_path(home, key)):
        assert key in p.parts[-2:] or p.name.startswith(key)
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    from maestro.view import board as board_vm
    view = board_vm.board(home, Config(home=home), "all")
    assert key not in [r.key for r in view.rows]  # T-301: reviews leave the ticket board
    assert cli.main(["dispatch", "--key", key]) == 0


def test_idempotent_and_distinct_keys(board, monkeypatch):
    home, _, _ = board
    vcs = _mint(board, monkeypatch, 7, "Same title")
    n = event_log.last_seq(home, "R-7-same-title")
    vcs.title = "A renamed title"  # a retitled PR still resolves to the minted ticket
    assert _review(vcs, 7, monkeypatch) == 0
    assert event_log.last_seq(home, "R-7-same-title") == n + 1  # the new title is observed, nothing else
    assert not (home / "events" / "R-7-a-renamed-title.jsonl").exists()
    _mint(board, monkeypatch, 8, "Same title")
    assert event_log.last_seq(home, "R-8-same-title") > 0


def test_untitled_falls_back_to_legacy_key(board, monkeypatch):
    home, _, _ = board
    _mint(board, monkeypatch, 9, "!!!")
    assert snap_mod.load(home, "review-acme-widgets-9").kind == "review"


def test_legacy_key_still_resolves(board, monkeypatch):
    home, _, _ = board
    _mint(board, monkeypatch, 9, None)
    n = event_log.last_seq(home, "review-acme-widgets-9")
    _mint(board, monkeypatch, 9, "Now it has a title")
    assert event_log.last_seq(home, "review-acme-widgets-9") == n + 1  # the new title is observed
    assert cli.main(["status"]) == 0
