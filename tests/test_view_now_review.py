"""T-252: `maestro.view.now` carries a brief-ready review as a `review` card and a running one in flight."""
from __future__ import annotations

import os

from conftest import seed_review_ticket
from maestro import claims, store
from maestro.config import Config
from maestro.view import now as now_view


def test_brief_ready_review_is_a_needs_you_review_card(home):
    seed_review_ticket(home)
    view = now_view.build(home, Config(home=home), store.now_epoch())
    assert [type(c) for c in view.needs_you] == [now_view.ReviewCard]
    card = view.needs_you[0]
    assert card.key == "T-60" and card.url.endswith("/pull/7") and card.author == "octo-reviewer"
    assert card.counts == {"blocking": 1, "question": 0, "nit": 1}
    assert card.verdict == "request-changes"
    assert not view.in_flight and not view.queued


def test_review_in_progress_is_an_in_flight_row(home):
    seed_review_ticket(home, brief=False, phase="qa")
    claims.write_claim(home, "T-60", os.getpid(), "test")
    view = now_view.build(home, Config(home=home), store.now_epoch())
    assert not view.needs_you
    row = view.in_flight[0]
    assert row.key == "T-60" and row.kind == "review" and row.phase == "qa"
