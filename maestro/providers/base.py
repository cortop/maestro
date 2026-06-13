"""Abstract provider interfaces. Concrete adapters live alongside in ``cli.py`` (or
a project's own module). The maestro core depends ONLY on these shapes.
"""
from __future__ import annotations

from typing import Protocol


class Tracker(Protocol):
    """An issue tracker (Jira, Linear, GitHub Issues, ...)."""

    def view(self, key: str) -> dict: ...
    def transition(self, key: str, status: str) -> None: ...
    def assignee(self, key: str) -> str | None: ...


class VCS(Protocol):
    """A code host (GitHub, GitLab, ...)."""

    def pr_for_branch(self, branch: str) -> dict | None: ...
    def ci_state(self, pr_number: int) -> str: ...


class Fetcher(Protocol):
    """Imports external work into maestro by writing to the ``_new`` inbox."""

    def fetch(self) -> int:
        """Run the import; return how many create-requests were enqueued."""


# --- Null implementations: the default, fully project-agnostic behaviour --------
class NullTracker:
    def view(self, key: str) -> dict: return {}
    def transition(self, key: str, status: str) -> None: pass
    def assignee(self, key: str) -> str | None: return None


class NullVCS:
    def pr_for_branch(self, branch: str) -> dict | None: return None
    def ci_state(self, pr_number: int) -> str: return "unknown"


class NullFetcher:
    def fetch(self) -> int: return 0
