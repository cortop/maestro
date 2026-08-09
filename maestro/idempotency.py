"""Deterministic step ids — the idempotency crux.

A step id is derived ONLY from log content (phase + observed sequence + action),
never from wall-clock or run id. So two reconcilers that race on the same frozen
log compute the *same* step id for the same next action; the event log's step-id
de-dup then collapses them to a single recorded effect (one PrOpened, not two).

This is what makes crash-and-respawn safe: a worker that died after writing its
event but before exiting is re-spawned, recomputes the identical step id, sees it
already present, and skips the external side effect.

Injectivity (RB-4): that guarantee only holds if the pre-hash encoding is
injective -- two distinct (key, phase, observed_seq, action) tuples must never
produce the same bytes, or a genuinely new action collapses onto an
already-applied step id and gets silently skipped. A plain "\x1f"-joined string
is NOT injective when a field can itself contain "\x1f" (e.g.
`step_id("K\x1fp", "q", 1, "a")` and `step_id("K", "p\x1fq", 1, "a")` used to
collide). The fix: when none of the four fields contains "\x1f" -- true of
every step id ever recorded before this fix, since `key` is regex-constrained
(`store._KEY_RE`) and `phase` is an enum value -- the payload is exactly the
old "\x1f"-joined string, byte for byte, so every historical digest is
unchanged: it joins the four fields with exactly three "\x1f" separators, and
since none of the fields contributes a "\x1f" of its own, that payload
contains *exactly* three "\x1f" bytes, always -- regardless of what the field
content otherwise is (including an empty `key`).

Only when a field does contain "\x1f" does the payload switch to a
length-prefixed ("netstring") encoding of all four fields with no separator
between them: each field is written as `f"{len(field)}:{field}"`, which is
unambiguous for arbitrary content because the length prefix says exactly how
many characters to consume next, so no byte sequence inside a field can ever
be mistaken for a field boundary (the same argument that makes netstrings/
bencode-style length-prefixing collision-safe). That payload is prefixed with
three literal "\x1f" bytes -- not the fields' own content, a fixed marker --
and `action` is rejected outright if it contains "\x1f" (see
`_reject_control_chars`), so this branch is only ever reached because `key`
or `phase` contains at least one "\x1f". The payload's total "\x1f" count is
therefore always >= 3 (marker) + 1 (the field content that triggered this
branch) = 4. Four is never three, so this branch's payload can *never* equal
a plain-join payload (which always has exactly three) -- for *any* key/phase/
action content, not just well-formed ones. (A single leading "\x1f" would not
be enough: a plain-join payload with an empty `key` also starts with "\x1f",
and if the remaining fields happen to look like a valid netstring, the two
encodings can coincide -- three fixed separators up front rules that out by
parity, since three field-content characters could never fill the position of
the join's three structural separators.)
"""
from __future__ import annotations

import hashlib

from . import store

_SEP = "\x1f"
_BRANCH_B_MARKER = _SEP * 3


def _reject_control_chars(name: str, value: str) -> None:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise store.MaestroError(f"step_id: {name} contains a control character: {value!r}")


def step_id(key: str, phase: str, observed_seq: int, action: str) -> str:
    """Hash (key, phase, observed_seq, action) into a 16-hex-char step id.

    See the module docstring for the injectivity argument. `action` is the
    one field a caller can populate with arbitrary text (`key` is validated
    by `store.validate_key`, `phase` is an enum value) -- belt-and-braces,
    reject it outright if it carries a control character rather than let it
    silently force the length-prefixed branch.
    """
    _reject_control_chars("action", action)
    fields = (key, phase, str(observed_seq), action)
    if any(_SEP in f for f in fields):
        payload = _BRANCH_B_MARKER + "".join(f"{len(f)}:{f}" for f in fields)
    else:
        payload = _SEP.join(fields)
    h = hashlib.sha256()
    h.update(payload.encode("utf-8"))
    return h.hexdigest()[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
