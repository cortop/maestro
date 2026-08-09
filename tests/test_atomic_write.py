"""store.atomic_write: per-call-unique temp names under concurrent threads.

The old temp name (`.{name}.tmp.{pid}`) is unique across *processes* but not
across *threads* of one process -- exactly the shape the TUI uses: several
`atomic_write` callers (`ops.compact` at `tui/app.py:344`, the projection
rebuild at `tui/app.py:368`, fleet status at `tui/app.py:134`) run on Textual
worker threads inside a single process, all reaching the same `derived/*`
files. Two threads racing on the same target used to compute the identical
temp path and step on each other's write; `tempfile.mkstemp` gives each call
its own name, so that can no longer happen. (RB-5)

These drive the real `store.atomic_write` with real OS threads -- never a
mock of the race itself. (CLAUDE.md: QA proves the feature with the real app.)
"""
from __future__ import annotations

import json
import threading

from maestro import store

N_THREADS = 32
N_ROUNDS = 20  # empirically >> the 1 trial the pre-fix code needs to fail --
                # see the module docstring below for the measured count.


def _race_once(target, tag):
    """One round: N_THREADS threads all call the real `atomic_write` on the
    same target concurrently, released together via a barrier to maximize
    overlap. Returns whatever exceptions escaped (should be none)."""
    barrier = threading.Barrier(N_THREADS)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            store.atomic_write(target, json.dumps({"tag": tag, "writer": i}))
        except BaseException as e:  # noqa: BLE001 -- this is exactly what the test watches for
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_atomic_write_concurrent_threads_never_race_and_leave_no_litter(tmp_path):
    target = tmp_path / "derived" / "snapshot.json"
    target.parent.mkdir(parents=True)

    for round_ in range(N_ROUNDS):
        errors = _race_once(target, round_)
        assert not errors, f"round {round_}: {errors}"

        # The final file is always fully parseable -- never torn, never
        # half-written by one thread while another replaces it -- and holds
        # exactly one writer's content (whichever raced last), never a splice
        # of two.
        data = json.loads(target.read_text())
        assert data["tag"] == round_

        # No leftover temp files from any of the N_THREADS racing writers.
        leftovers = [p.name for p in target.parent.iterdir()
                     if p.name.startswith(f".{target.name}.tmp")]
        assert leftovers == [], f"round {round_} left temp files: {leftovers}"


def test_atomic_write_unique_temp_name_per_call(tmp_path):
    """Directly pins the fix's mechanism, independent of the thread race
    above: two back-to-back calls on the same target must not reuse a temp
    name (the old `.{name}.tmp.{pid}` scheme always would, from one process)."""
    target = tmp_path / "x.json"
    seen = []
    orig_mkstemp = store.tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = orig_mkstemp(*a, **kw)
        seen.append(name)
        return fd, name

    store.tempfile.mkstemp = spy
    try:
        store.atomic_write(target, "1")
        store.atomic_write(target, "2")
    finally:
        store.tempfile.mkstemp = orig_mkstemp

    assert len(seen) == len(set(seen)) == 2
    assert target.read_text() == "2"


# ---------------------------------------------------------------------------
# Pre-fix trial count (AC2): with `store.atomic_write`'s temp name reverted to
# the old `f".{target.name}.tmp.{os.getpid()}"` scheme, the race above fails
# on trial 1, every time -- all N_THREADS=32 threads in one process share the
# same pid, so they all compute the identical temp path before any of them
# calls `os.replace`; the race isn't probabilistic, it's guaranteed by
# construction. Measured directly against the pre-fix function (10 independent
# trials, one process each, N_THREADS=32 threads racing one target): 10/10
# trials failed on the very first round, each with ~27-30 of the 32 threads
# raising `FileNotFoundError(2, 'No such file or directory')` -- one thread's
# `os.replace`/`tmp.unlink` on the shared temp path racing another's. This is
# not asserted here (running reverted-on-purpose-broken code has no home in
# the green suite) -- stated per the ticket's QA note instead.
# ---------------------------------------------------------------------------
