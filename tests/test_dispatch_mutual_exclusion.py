"""T-52: dispatch() has no mutual exclusion, and the claim used to be written
after Popen -- together, two overlapping sweeps (launchd's timer tick racing
the in-process `cli._nudge` fired on every `maestro ans`/`cmd`/`create`) could
both read a stale `active` set, both decide the same key had a free slot, and
both spawn it: the second `write_claim` clobbered the first, leaving one live
reconciler with NO claim at all -- invisible to `max_concurrency` and to
`run_watchdog` (which iterates claims only), so the orphan was never reaped.

This reproduces the race with two real THREADS calling the real `dispatch()`
concurrently against one temp home: `store.file_lock` is a real `fcntl.flock`
on a sidecar file, which blocks a second open file description exactly like it
would a second OS process's -- so the race is genuine, not simulated. Confirms
AC (a)/(b): pre-fix (no lock around the active-read/compute-slots/spawn
region) this test double-spawns the due key; post-fix, only one of the two
concurrent calls ever spawns it.
"""
from __future__ import annotations

import os
import threading
import time

from maestro import claims, dispatcher as disp, event_log, snapshot as snap_mod, store
from maestro.config import Config
from maestro.statemachine import Phase


def _seed(home, key, phase=Phase.READY):
    store.atomic_write(store.spec_path(home, key),
                        f"# {key}\napproval_tier: 0\n\n## Acceptance criteria\n- [ ] ok\n")
    event_log.append(home, key, "TicketCreated",
                     {"title": key, "spec_hash": disp.spec_hash_on_disk(home, key)},
                     actor="d")
    event_log.append(home, key, "PhaseChanged", {"phase": phase.value}, actor="r")
    snap_mod.rebuild(home, key)


class _RacySessions:
    """A real SessionManager (real claim files, real ``ps``-backed liveness via
    ``claims.active_keys``), with an injected delay inside ``list_active()`` to
    widen the active-read-to-spawn window a genuine race needs -- exactly the
    window T-52 (1)/(2) describes. ``spawn()`` writes a real claim under THIS
    TEST PROCESS's own pid (a real, live process the ``ps``-backed claims
    machinery can confirm as alive), and records every call so the test can
    assert how many actually happened.
    """

    def __init__(self, home, delay: float = 0.15):
        self.home = home
        self.delay = delay
        self.spawn_calls: list[str] = []
        self._record_lock = threading.Lock()

    def list_active(self) -> set[str]:
        active = claims.active_keys(self.home)
        time.sleep(self.delay)  # widen the window -- the point of this fixture
        return active

    def spawn(self, key, command, cwd, **kwargs):
        with self._record_lock:
            self.spawn_calls.append(key)
        claims.write_claim(self.home, key, os.getpid(), f"reconcile-{key}")
        return os.getpid()


def test_two_concurrent_dispatch_sweeps_never_double_spawn_same_key(home):
    _seed(home, "T-1", Phase.READY)
    cfg = Config(home=home, max_concurrency=5)
    sessions = _RacySessions(home)

    reports: list[disp.DispatchReport] = []
    reports_lock = threading.Lock()

    def run():
        report = disp.dispatch(cfg, sessions, now=1000)
        with reports_lock:
            reports.append(report)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "a dispatch() call never returned"

    assert len(reports) == 2
    count = sessions.spawn_calls.count("T-1")
    assert count == 1, (
        f"T-1 was spawned {count} times across two concurrent sweeps -- the "
        "spawn-region lock did not serialize dispatch()'s active-read/"
        "compute-slots/spawn decision")

    # The claim landed, and points at the one real spawn -- never orphaned by a
    # second write clobbering the first (T-52 (2)'s concrete failure shape).
    claim = claims.read_claim(home, "T-1")
    assert claim is not None
    assert claim["pid"] == os.getpid()

    # One sweep spawned it; the other must have seen it already active/claimed,
    # never a second independent "due" decision for the same key.
    spawned_reports = [r for r in reports if "T-1" in r.spawned]
    claimed_reports = [r for r in reports if "T-1" in r.claimed]
    assert len(spawned_reports) == 1
    assert len(claimed_reports) == 1
