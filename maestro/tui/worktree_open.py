"""Open a ticket's worktree in cmux, a new Apple Terminal tab, or report its path."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from .. import store

Runner = Callable[[list[str]], None]


def _spawn(argv: list[str]) -> None:
    """Default runner: fire-and-forget; the launcher detaches from the TUI's terminal."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# The injectable seam: tests replace this to observe the external spawn.
runner: Runner = _spawn


def open_worktree(app, home, key: str) -> None:
    """Open `key`'s worktree for the operator, or notify why it cannot be opened."""
    wt = store.worktree_path(home, key)
    if not wt.is_dir():
        app.notify(f"No worktree for {key}", severity="warning")
        return
    if os.environ.get("CMUX_SOCKET_PATH") and shutil.which("cmux"):
        argv = ["cmux", str(wt)]
    elif os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        argv = ["open", "-a", "Terminal", str(wt)]
    else:
        app.notify(f"Worktree for {key}: {wt}")
        return
    try:
        runner(argv)
    except OSError as e:
        app.notify(f"Could not open worktree for {key}: {e}", severity="warning")
        return
    app.notify(f"Opened worktree for {key}")
