"""T-254: reviews on the TUI's Now tab -- brief, finding edits, pending review, palette and footer.

Real `MaestroTUI` driven with real keys over a temp home; only the code-host boundary (`FakeVCS`)
is faked. Fixtures are synthetic.
"""
from __future__ import annotations

import asyncio

from textual.widgets import Input, Static
from textual.widgets._footer import FooterKey

from conftest import seed_review_ticket, seed_ticket
from maestro import event_log, snapshot as snap_mod
from maestro.tui import _ConfirmModal, _InboxModal
from tui_support import _make_app

KEY, HEAD = "T-60", "a" * 40


class FakeVCS:
    def __init__(self, *, head=HEAD):
        self.head = head
        self.created: list[dict] = []

    def pr_head(self, pr_number, repo=None, env=None):
        return {"head_sha": self.head, "state": "OPEN", "is_fork": False, "error": None}

    def pending_review(self, pr_number, repo=None, env=None):
        return {"ok": True, "review_id": None}

    def pr_diff_lines(self, pr_number, repo=None, env=None):
        return {"ok": True, "lines": {"src/a.py": [2, 3, 4]}}

    def create_pending_review(self, pr_number, head_sha, body, comments, repo=None, env=None):
        self.created.append({"head_sha": head_sha, "body": body, "comments": comments})
        return {"ok": True, "id": "9001"}


def _run(home, body, vcs=None):
    async def _inner():
        app = _make_app(home)
        app._nudge_enabled = False
        app._vcs = vcs
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(pilot)
            await body(app, pilot)
            assert app._exception is None
    asyncio.run(_inner())


async def _settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause(0.2)


def _detail(app) -> str:
    return str(app.query_one("#now-detail", Static).render())


def _groups(app) -> str:
    return str(app.query_one("#now-groups", Static).render())


def _footer(app) -> list[str]:
    return [k.action for k in app.screen.query(FooterKey)]


def test_brief_ready_review_shows_summary_checks_and_findings(home):
    seed_review_ticket(home)

    async def body(app, pilot):
        assert "NEEDS YOU (1)" in _groups(app) and "review" in _groups(app)
        d = _detail(app)
        assert "request-changes" in d and "Adds a <b>parser</b>." in d
        assert "Tests passed." in d
        assert "blocking" in d and "src/a.py:3" in d and "Please guard the empty case." in d
        assert "nit" in d and "src/b.py:9" in d and "Is the API ok?" not in d
        assert "Pick it up" not in d
    _run(home, body)


def test_toggle_edit_and_create_pending_review_after_confirm(home):
    seed_review_ticket(home)
    vcs = FakeVCS()

    async def body(app, pilot):
        await pilot.press("right", "space")  # exclude the nit
        await pilot.pause()
        assert "[ ]" in _detail(app) and "1 of 2 included" in _detail(app)
        await pilot.press("left", "u")
        await pilot.pause()
        modal = app.screen_stack[-1]
        assert isinstance(modal, _InboxModal)
        assert modal.query_one("#inbox-input", Input).value == "Please guard the empty case."
        modal.dismiss("Guard the empty case, then retest.")
        await pilot.pause()
        assert "(edited)" in _detail(app) and "Guard the empty case, then retest." in _detail(app)
        await pilot.press("C")
        await pilot.pause()
        assert isinstance(app.screen_stack[-1], _ConfirmModal)
        assert vcs.created == []  # nothing is sent before the confirm
        await pilot.press("y")
        await _settle(pilot)
        assert vcs.created == [{"head_sha": HEAD, "body": "", "comments": [
            {"path": "src/a.py", "line": 3, "body": "Guard the empty case, then retest."}]}]
        snap = snap_mod.load(home, KEY)
        assert snap.phase == "awaiting-ci" and snap.review_pending["review_id"] == "9001"
        assert "NEEDS YOU (0)" in _groups(app)
    _run(home, body, vcs)


def test_cancelled_confirm_creates_nothing(home):
    seed_review_ticket(home)
    vcs = FakeVCS()

    async def body(app, pilot):
        await pilot.press("C")
        await pilot.pause()
        await pilot.press("n")
        await _settle(pilot)
        assert vcs.created == [] and snap_mod.load(home, KEY).phase == "awaiting-human"
    _run(home, body, vcs)


def test_refusal_is_shown_and_nothing_is_created(home):
    seed_review_ticket(home)
    vcs = FakeVCS(head="b" * 40)  # the PR moved on since the brief

    async def body(app, pilot):
        await pilot.press("C")
        await pilot.pause()
        await pilot.press("y")
        await _settle(pilot)
        assert vcs.created == [] and snap_mod.load(home, KEY).phase == "awaiting-human"
        assert any("new commits" in n.message for n in app._notifications)
    _run(home, body, vcs)


def test_excluding_every_finding_refuses_before_the_confirm(home):
    seed_review_ticket(home)

    async def body(app, pilot):
        await pilot.press("space", "right", "space", "C")
        await pilot.pause()
        assert not isinstance(app.screen_stack[-1], _ConfirmModal)
    _run(home, body, FakeVCS())


def test_footer_and_palette_offer_review_actions_only_on_a_review_row(home):
    seed_review_ticket(home)
    seed_ticket(home, "Q-1", "needs an answer", phase="awaiting-human", questions={"q1": "Go?"})

    async def labels(app, pilot):
        from textual.command import CommandList
        await pilot.press("ctrl+p")
        for _ in range(100):
            await pilot.pause(0.05)
            lst = app.screen.query_one(CommandList)
            if lst.option_count:
                break
        out = [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]
        await pilot.press("escape")
        await pilot.pause()
        return out

    async def body(app, pilot):
        first = app._now_card().key
        review_first = first == KEY
        if not review_first:
            await pilot.press("down")
            await _settle(pilot)
        acts = _footer(app)
        assert {"review_toggle", "review_edit", "review_create"} <= set(acts) and "answer" not in acts
        on_review = await labels(app, pilot)
        assert any(l.startswith("Review a PR (") for l in on_review)
        assert any(l.startswith("Create pending review (") for l in on_review)
        await pilot.press("up" if not review_first else "down")
        await _settle(pilot)
        acts = _footer(app)
        assert "answer" in acts and not any(a.startswith("review_") for a in acts), acts
        off_review = await labels(app, pilot)
        assert any(l.startswith("Review a PR (") for l in off_review)
        assert not any(l.startswith(("Create pending review", "Include/exclude", "Edit finding"))
                       for l in off_review), off_review
        await pilot.press("2")
        await _settle(pilot)
        assert not any(a.startswith("review_") for a in _footer(app))
    _run(home, body)


def test_review_a_pr_from_the_palette_action_mints_a_review_ticket(home):
    async def body(app, pilot):
        await pilot.press("V")
        await pilot.pause()
        modal = app.screen_stack[-1]
        assert isinstance(modal, _InboxModal)
        modal.dismiss("https://github.com/acme/widgets/pull/9")
        await _settle(pilot)
        key = "review-acme-widgets-9"
        assert event_log.last_seq(home, key) > 0
        assert snap_mod.load(home, key).kind == "review"
    _run(home, body, FakeVCS())
