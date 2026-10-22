"""Change hub for `/stream` -- one poller per process fans named change events out to subscribers."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

_WATCHED = (("derived/snapshots", ".json"), ("events", ".jsonl"), ("inbox", ".jsonl"))


def signatures(home: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` over the snapshot, event-log and inbox files --
    the same change detection the TUI board poll uses."""
    out: dict[str, tuple[int, int]] = {}
    for sub, suffix in _WATCHED:
        d = home / sub
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.endswith(suffix) and not e.name.startswith("."):
                try:
                    st = e.stat()
                except OSError:
                    continue
                out[f"{sub}/{e.name}"] = (st.st_size, st.st_mtime_ns)
    return out


def key_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    for suffix in (".archive.jsonl", ".jsonl", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def diff(old: dict, new: dict) -> list[str]:
    """Event names for the delta: ``ticket:<KEY>`` per changed key, then ``board`` and
    ``fleet`` when anything changed."""
    changed = {rel for rel in old.keys() | new.keys() if old.get(rel) != new.get(rel)}
    if not changed:
        return []
    names = [f"ticket:{k}" for k in sorted({key_of(r) for r in changed})]
    return names + ["board", "fleet"]


class Hub:
    """Owns the poll loop, the monotonic event-id counter and the subscriber queues."""

    def __init__(self, home: Path, interval: float = 1.0) -> None:
        self.home = home
        self.interval = interval
        self.next_id = 1
        self._subs: set[asyncio.Queue] = set()
        self._sigs: dict[str, tuple[int, int]] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._sigs = await asyncio.to_thread(signatures, self.home)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def publish(self, name: str) -> tuple[int, str]:
        ev = (self.next_id, name)
        self.next_id += 1
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            new = await asyncio.to_thread(signatures, self.home)
            for name in diff(self._sigs, new):
                self.publish(name)
            self._sigs = new

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def keys(self) -> list[str]:
        return sorted({key_of(r) for r in self._sigs if r.startswith("events/")
                       and not r.endswith(".archive.jsonl")})

    def full_refresh(self) -> list[tuple[int, str]]:
        """A reconnecting client's catch-up: every ticket, the board and the fleet panel."""
        return [self.publish(n) for n in [*(f"ticket:{k}" for k in self.keys()), "board", "fleet"]]
