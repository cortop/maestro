"""T-125 (b): `review_noise_patterns`/`review_noise_authors` -- an opt-in,
empty-by-default pre-filter in `dispatcher._observe_reviews`. A matching
COMMENTED/APPROVED/INLINE_COMMENT body still records ReviewFeedbackReceived
(history stays complete) but contributes nothing to the routing reason, and a
`skipped as noise` Note is appended instead; CHANGES_REQUESTED is never
filtered, regardless of either list. Empty defaults must leave every existing
`_observe_reviews` behavior byte-identical (AC3).
"""
import pytest

from maestro import config as config_mod, dispatcher as disp, event_log, providers, \
    snapshot as snap_mod, store
from maestro.statemachine import Phase


class FakeVCS:
    """The only mock: the external GitHub review-feedback boundary."""

    def __init__(self, reviews):
        self.reviews = reviews

    def pr_for_branch(self, branch, repo=None, env=None):
        return None

    def pr_status(self, pr_number, repo=None, env=None):
        return {"state": "OPEN", "mergeable": "MERGEABLE", "head_sha": "sha1",
                "ci_state": "passing", "failing_checks": []}

    def review_feedback(self, pr_number, repo=None, env=None):
        return self.reviews


def _seed(cfg, key, phase=Phase.IN_REVIEW, pr=42):
    store.atomic_write(
        store.spec_path(cfg.home, key),
        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n",
    )
    event_log.append(cfg.home, key, "TicketCreated", {"title": key}, actor="d")
    event_log.append(cfg.home, key, "PrOpened",
                     {"number": pr, "url": f"https://github.com/x/y/pull/{pr}", "draft": False},
                     actor="r")
    event_log.append(cfg.home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    return snap_mod.rebuild(cfg.home, key)


def _use_fake(cfg, monkeypatch, fake):
    cfg.providers["vcs"] = "github_cli"
    cfg.provider_config = {"vcs": {"github_cli": {"sync_interval": 0}}}
    monkeypatch.setattr(providers, "get_vcs", lambda c: fake)


def _notes(home, key):
    return [e for e in event_log.read(home, key) if e["type"] == "Note"]


def _implementing_reasons(home, key):
    return [e["payload"].get("reason", "") for e in event_log.read(home, key)
            if e["type"] == "PhaseChanged" and e["payload"].get("phase") == Phase.IMPLEMENTING.value]


# ---------------------------------------------------------------------------
# AC3: unset (default) config -- byte-identical to before this ticket
# ---------------------------------------------------------------------------

def test_default_empty_lists_route_commented_review_unfiltered(cfg, monkeypatch):
    fake = FakeVCS([{"id": "cm-1", "state": "COMMENTED",
                     "body": "consider adding a docstring here", "author": "reviewer2"}])
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5")
    assert cfg.review_noise_patterns == []
    assert cfg.review_noise_authors == []

    disp.sync_vcs(cfg, now=1000)

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert _implementing_reasons(cfg.home, "T-5")[-1] == \
        "review comment: consider adding a docstring here"
    assert _notes(cfg.home, "T-5") == []


def test_default_empty_lists_route_approved_with_inline_comments_unfiltered(cfg, monkeypatch):
    fake = FakeVCS([
        {"id": "ap-1", "state": "APPROVED", "body": "nice work", "author": "reviewer1"},
        {"id": "il-1", "state": "INLINE_COMMENT", "body": "typo here",
         "path": "maestro/foo.py", "line": 12, "author": "reviewer1"},
    ])
    _use_fake(cfg, monkeypatch, fake)
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    reason = _implementing_reasons(cfg.home, "T-5")[-1]
    assert reason.startswith("approved with comments: ")
    assert "nice work" in reason and "typo here" in reason
    assert _notes(cfg.home, "T-5") == []


# ---------------------------------------------------------------------------
# AC4: a matching pattern filters COMMENTED/APPROVED/INLINE_COMMENT, never
# CHANGES_REQUESTED
# ---------------------------------------------------------------------------

def test_pattern_matching_approved_body_routes_nothing_but_records_history(cfg, monkeypatch):
    fake = FakeVCS([{"id": "ap-1", "state": "APPROVED", "body": "LGTM", "author": "bot1"}])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_patterns = ["LGTM"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IN_REVIEW.value  # routed nothing
    events = event_log.read(cfg.home, "T-5")
    review_evs = [e for e in events if e["type"] == "ReviewFeedbackReceived"]
    assert len(review_evs) == 1 and review_evs[0]["payload"]["body"] == "LGTM"  # history stays complete
    notes = _notes(cfg.home, "T-5")
    assert len(notes) == 1
    assert notes[0]["payload"]["text"] == "review comment skipped as noise (LGTM): LGTM"


def test_changes_requested_matching_same_pattern_still_routes(cfg, monkeypatch):
    fake = FakeVCS([{"id": "cr-1", "state": "CHANGES_REQUESTED", "body": "LGTM", "author": "bot1"}])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_patterns = ["LGTM"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    snap = snap_mod.load(cfg.home, "T-5")
    assert snap.phase == Phase.IMPLEMENTING.value
    assert _implementing_reasons(cfg.home, "T-5")[-1] == "changes requested: LGTM"
    assert _notes(cfg.home, "T-5") == []  # never filtered -> no skip Note


def test_pattern_matching_commented_body_is_skipped_with_note(cfg, monkeypatch):
    fake = FakeVCS([{"id": "cm-1", "state": "COMMENTED", "body": "bump", "author": "dependabot"}])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_patterns = [r"bump"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value
    notes = _notes(cfg.home, "T-5")
    assert len(notes) == 1
    assert "skipped as noise" in notes[0]["payload"]["text"]


def test_pattern_matching_inline_comment_is_skipped_but_approval_still_routes_on_body(cfg, monkeypatch):
    """A noise-filtered INLINE_COMMENT nulls only its own contribution -- a
    same-round APPROVED body that isn't itself noise still routes."""
    fake = FakeVCS([
        {"id": "ap-1", "state": "APPROVED", "body": "great, one nit below", "author": "reviewer1"},
        {"id": "il-1", "state": "INLINE_COMMENT", "body": "bump",
         "path": "maestro/foo.py", "line": 3, "author": "reviewer1"},
    ])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_patterns = [r"bump"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    reason = _implementing_reasons(cfg.home, "T-5")[-1]
    assert reason == "approved with comments: great, one nit below"
    assert "bump" not in reason
    notes = _notes(cfg.home, "T-5")
    assert len(notes) == 1 and "skipped as noise" in notes[0]["payload"]["text"]


def test_author_listed_filters_regardless_of_body_text(cfg, monkeypatch):
    fake = FakeVCS([{"id": "cm-1", "state": "COMMENTED",
                     "body": "anything at all here", "author": "github-actions[bot]"}])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_authors = ["github-actions[bot]"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IN_REVIEW.value
    notes = _notes(cfg.home, "T-5")
    assert len(notes) == 1
    assert notes[0]["payload"]["text"] == \
        "review comment skipped as noise (author:github-actions[bot]): anything at all here"


def test_pattern_must_fullmatch_not_substring(cfg, monkeypatch):
    """A pattern is a fullmatch, not a search -- a real comment that merely
    contains the pattern as a substring is never silently swallowed."""
    fake = FakeVCS([{"id": "cm-1", "state": "COMMENTED",
                     "body": "LGTM but please also fix the typo", "author": "reviewer1"}])
    _use_fake(cfg, monkeypatch, fake)
    cfg.review_noise_patterns = ["LGTM"]
    _seed(cfg, "T-5")

    disp.sync_vcs(cfg, now=1000)

    assert snap_mod.load(cfg.home, "T-5").phase == Phase.IMPLEMENTING.value
    assert _notes(cfg.home, "T-5") == []


# ---------------------------------------------------------------------------
# AC5: a malformed regex fails config.load() closed, naming the knob
# ---------------------------------------------------------------------------

def test_malformed_regex_fails_config_load_closed(home):
    (home / "config.toml").write_text(
        '[maestro]\nreview_noise_patterns = ["["]\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    msg = str(exc.value)
    assert "review_noise_patterns" in msg


def test_non_string_pattern_entry_fails_config_load_closed(home):
    (home / "config.toml").write_text(
        '[maestro]\nreview_noise_patterns = [1]\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    assert "review_noise_patterns" in str(exc.value)


def test_non_list_authors_fails_config_load_closed(home):
    (home / "config.toml").write_text(
        '[maestro]\nreview_noise_authors = "not-a-list"\n', encoding="utf-8")
    with pytest.raises(store.MaestroError) as exc:
        config_mod.load(str(home))
    assert "review_noise_authors" in str(exc.value)


def test_valid_patterns_and_authors_load_cleanly(home):
    (home / "config.toml").write_text(
        '[maestro]\nreview_noise_patterns = ["LGTM", "bump.*"]\n'
        'review_noise_authors = ["github-actions[bot]"]\n', encoding="utf-8")
    cfg = config_mod.load(str(home))
    assert cfg.review_noise_patterns == ["LGTM", "bump.*"]
    assert cfg.review_noise_authors == ["github-actions[bot]"]
