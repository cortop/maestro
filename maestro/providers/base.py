"""Abstract provider interfaces. Concrete adapters live alongside in ``cli.py`` (or
a project's own module). The maestro core depends ONLY on these shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Tracker(Protocol):
    """An issue tracker (Jira, Linear, GitHub Issues, ...).

    ``import_new``/``refresh`` are the opt-in external-sync half (see
    ``dispatcher.sync_external_sources``) — a tracker with nothing to sync can
    leave them as no-ops (see ``NullTracker``).
    """

    def view(self, key: str) -> dict: ...
    def transition(self, key: str, status: str) -> None: ...
    def assignee(self, key: str) -> str | None: ...

    def import_new(self, home: Path) -> int:
        """Pull new external work into the ``_new`` inbox; return how many
        create-requests were enqueued."""
        ...

    def refresh(self, home: Path, key: str, external_id: str) -> int:
        """Refresh one tracked, not-done ticket from the external source;
        return how many events were appended (0 if nothing changed)."""
        ...


class VCS(Protocol):
    """A code host (GitHub, GitLab, ...)."""

    def pr_for_branch(self, branch: str) -> dict | None: ...

    def pr_status(self, pr_number: int) -> dict:
        """Poll one PR's merge state and CI checks in a single round-trip.

        Returns {"state": "OPEN"|"MERGED"|"CLOSED"|"unknown",
        "mergeable": "MERGEABLE"|"CONFLICTING"|"UNKNOWN", "head_sha": str|None,
        "ci_state": "passing"|"failing"|"pending"|"unknown",
        "failing_checks": [str, ...]}.
        """
        ...

    def review_feedback(self, pr_number: int) -> list[dict]:
        """Return every review left on the PR as [{"id": str, "state": str|None,
        "body": str, "author": str|None}, ...]. The caller de-dupes per comment-id
        via the event log's step-id idempotency (see ``dispatcher.sync_vcs``)."""
        ...


class Fetcher(Protocol):
    """Imports external work into maestro by writing to the ``_new`` inbox."""

    def fetch(self) -> int:
        """Run the import; return how many create-requests were enqueued."""


# --- Null implementations: the default, fully project-agnostic behaviour --------
class NullTracker:
    def view(self, key: str) -> dict: return {}
    def transition(self, key: str, status: str) -> None: pass
    def assignee(self, key: str) -> str | None: return None
    def import_new(self, home: Path) -> int: return 0
    def refresh(self, home: Path, key: str, external_id: str) -> int: return 0


class NullVCS:
    def pr_for_branch(self, branch: str) -> dict | None: return None
    def pr_status(self, pr_number: int) -> dict:
        return {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                "ci_state": "unknown", "failing_checks": []}
    def review_feedback(self, pr_number: int) -> list[dict]: return []


class NullFetcher:
    def fetch(self) -> int: return 0
