"""Deterministic fault injection over store.py's OS boundary. (RB-11)

Not a kill-the-process harness -- an in-process shim. `store.append_line`,
`store.atomic_write` and `store.file_lock` are the only places the package
touches the OS durably; each funnels through a small, fixed set of primitives
(`os.fsync`, `os.replace`, `fcntl.flock`, and the raw `Path.open(...).write()`
that writes a line/file's bytes). This module monkeypatches exactly those
primitives so a test can make the *ordinal*-th call to one of them misbehave
-- deterministically, in milliseconds, with no subprocess and no platform
tricks. `event_log.append`, `ops.compact`, `snapshot.fold`/`rebuild` and the
CLI verbs under test stay the real implementations throughout; this replaces
only what they'd otherwise ask the OS to do (CLAUDE.md: mock only the
genuinely external boundary).

Call ordinals are counted per primitive, globally for the life of one
`FaultInjector` -- construct a fresh one per test (the `faults` fixture in
conftest.py does this). Arm at most the one fault a test cares about via
`inject_*`; every call to that primitive other than the armed ordinal passes
through to the real syscall untouched.
"""
from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest


class InjectedCrash(OSError):
    """Raised in place of a real OS failure at the armed injection point --
    a stand-in for "the process died right here." Subclasses OSError so
    existing `pytest.raises(OSError)` call sites keep working unchanged."""


@dataclass
class _TruncatingFile:
    """Wraps a real text-mode file object; the injector's armed write call
    writes only a truncated prefix of its argument, flushes that prefix to
    the OS (so it is visible to a subsequent read in this same process, the
    in-process stand-in for "the bytes reached the page cache before the
    crash"), then raises -- reproducing a writer that died mid-syscall and
    left an unterminated line on disk (RB-1's shape), generalized to any
    `append_line`/`atomic_write` caller rather than a hand-crafted fixture."""

    _real: object
    _injector: "FaultInjector"

    def write(self, s):
        self._injector._write_n += 1
        if self._injector._write_fault == self._injector._write_n:
            prefix = s[: max(1, len(s) // 2)]
            self._real.write(prefix)
            self._real.flush()
            raise InjectedCrash(
                f"[fault-injection] write #{self._injector._write_n} truncated mid-line"
            )
        return self._real.write(s)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


@dataclass
class FaultInjector:
    """Construct via the `faults` fixture, not directly -- it needs a live
    `pytest.MonkeyPatch` to install and auto-revert the patches."""

    monkeypatch: pytest.MonkeyPatch
    _fsync_n: int = field(default=0, init=False)
    _replace_n: int = field(default=0, init=False)
    _write_n: int = field(default=0, init=False)
    _fsync_fault: tuple[int, str] | None = field(default=None, init=False)  # (ordinal, "raise"|"noop")
    _replace_fault: int | None = field(default=None, init=False)
    _write_fault: int | None = field(default=None, init=False)
    _flock_unavailable: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        real_fsync = os.fsync
        real_replace = os.replace
        real_flock = fcntl.flock
        real_open = Path.open

        def patched_fsync(fd):
            self._fsync_n += 1
            if self._fsync_fault and self._fsync_fault[0] == self._fsync_n:
                _, mode = self._fsync_fault
                if mode == "raise":
                    raise InjectedCrash(f"[fault-injection] fsync #{self._fsync_n}")
                return None  # "noop": claim success without actually flushing
            return real_fsync(fd)

        def patched_replace(src, dst):
            self._replace_n += 1
            if self._replace_fault == self._replace_n:
                raise InjectedCrash(f"[fault-injection] os.replace #{self._replace_n}")
            return real_replace(src, dst)

        def patched_flock(fd, op):
            if self._flock_unavailable:
                raise InjectedCrash(
                    "[fault-injection] flock unavailable (e.g. NFS/SMB without locking)"
                )
            return real_flock(fd, op)

        def patched_open(path_self, mode="r", *a, **kw):
            f = real_open(path_self, mode, *a, **kw)
            if self._write_fault is not None and ("a" in mode or "w" in mode) and "b" not in mode:
                return _TruncatingFile(f, self)
            return f

        # Patch the primitives themselves (shared module objects), the same
        # seam RB-1's and RB-6's own crash tests already use -- never
        # event_log/ops/snapshot, which stay real.
        self.monkeypatch.setattr(os, "fsync", patched_fsync)
        self.monkeypatch.setattr(os, "replace", patched_replace)
        self.monkeypatch.setattr(fcntl, "flock", patched_flock)
        self.monkeypatch.setattr(Path, "open", patched_open)

    # -- arm a fault; a test typically arms exactly one, then makes the real
    # call it expects to crash at that point. Arming resets that primitive's
    # own counter, so `ordinal` always counts from "now" -- calls the test
    # already made before arming (e.g. seeding fixture events) never count
    # against it, only calls made by the real operation under test after --
    def inject_fsync_raise(self, ordinal: int) -> None:
        self._fsync_n = 0
        self._fsync_fault = (ordinal, "raise")

    def inject_fsync_noop(self, ordinal: int) -> None:
        self._fsync_n = 0
        self._fsync_fault = (ordinal, "noop")

    def inject_replace_raise(self, ordinal: int = 1) -> None:
        self._replace_n = 0
        self._replace_fault = ordinal

    def inject_truncated_write(self, ordinal: int = 1) -> None:
        self._write_n = 0
        self._write_fault = ordinal

    def inject_flock_unavailable(self) -> None:
        self._flock_unavailable = True
