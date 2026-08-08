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
    """A code host (GitHub, GitLab, ...). PR numbers are only unique WITHIN a repo,
    so every call takes an optional ``repo`` slug (e.g. "owner/repo"); ``None`` means
    "use the provider's own default" (today's single-repo/iterate-all behavior), so
    single-repo boards are unaffected. Callers resolve the slug via ``repos.py``.
    """

    def pr_for_branch(self, branch: str, repo: str | None = None) -> dict | None: ...

    def pr_status(self, pr_number: int, repo: str | None = None) -> dict:
        """Poll one PR's merge state and CI checks in a single round-trip.

        Returns {"state": "OPEN"|"MERGED"|"CLOSED"|"unknown",
        "mergeable": "MERGEABLE"|"CONFLICTING"|"UNKNOWN", "head_sha": str|None,
        "ci_state": "passing"|"failing"|"pending"|"unknown",
        "failing_checks": [str, ...],
        "error": "auth"|"not_found"|"transient"|"unknown" (optional)}.

        ``error`` is a NEW field, absent (or ``None``) on a successful poll --
        callers must read it with ``.get("error")`` so implementations/fakes that
        predate it keep behaving as "no error". When the poll itself couldn't be
        completed (the underlying `gh`/host-CLI call failed), ``ci_state`` still
        collapses to its existing four values -- it is never overloaded -- and
        ``error`` carries WHY: "auth" (bad/expired credentials, SSO not granted),
        "not_found" (repo or PR doesn't resolve), "transient" (timeout/network --
        retry, don't spend failure budget), or "unknown" (unrecognized failure --
        today's behavior, unchanged).
        """
        ...

    def review_feedback(self, pr_number: int, repo: str | None = None) -> list[dict]:
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
    def pr_for_branch(self, branch: str, repo: str | None = None) -> dict | None: return None
    def pr_status(self, pr_number: int, repo: str | None = None) -> dict:
        return {"state": "unknown", "mergeable": "UNKNOWN", "head_sha": None,
                "ci_state": "unknown", "failing_checks": []}
    def review_feedback(self, pr_number: int, repo: str | None = None) -> list[dict]: return []


class NullFetcher:
    def fetch(self) -> int: return 0
