"""append_line's crash-safety: a torn tail must never swallow the next append.

A previous writer dying mid-line leaves the target's last byte not a ``\\n``.
These tests drive ``store.append_line`` directly (and, for the event log and
inbox callers, through the real appenders) to prove a torn tail can no longer
corrupt or swallow the append that follows it. (RB-1)

RB-11 note: these fix the torn tail in place as a static precondition (a
hand-crafted fixture), covering `append_line`'s recovery logic in isolation.
`tests/test_crash_injection.py` generalizes this: the torn tail there is
produced by an actual injected crash mid-write during a real
`event_log.append` call, across every window the ticket lists, not just this
one. Kept separately -- different unit (`append_line` alone vs. the full
`event_log.append` call), different thing proved (recovery-from-artifact vs.
crash-during-the-call).
"""
from maestro import inbox, store


def _write_raw(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write(data)


def test_append_line_to_empty_file_writes_no_leading_newline(tmp_path):
    target = tmp_path / "events" / "T-1.jsonl"
    store.append_line(target, '{"seq": 1}')
    assert target.read_bytes() == b'{"seq": 1}\n'


def test_append_line_after_well_formed_line_writes_no_extra_newline(tmp_path):
    target = tmp_path / "events" / "T-1.jsonl"
    store.append_line(target, '{"seq": 1}')
    store.append_line(target, '{"seq": 2}')
    assert target.read_bytes() == b'{"seq": 1}\n{"seq": 2}\n'


def test_append_line_after_torn_tail_prepends_newline(tmp_path):
    target = tmp_path / "events" / "T-1.jsonl"
    _write_raw(target, b'{"seq": 1}\n')
    # a crashed writer: no trailing newline
    _write_raw(target, b'{"seq": 2, "payload": {"t": "torn"')
    store.append_line(target, '{"seq": 3}')

    lines = target.read_bytes().split(b"\n")
    # the torn fragment is left exactly as it was -- additive, never rewritten
    assert lines[1] == b'{"seq": 2, "payload": {"t": "torn"'
    assert lines[2] == b'{"seq": 3}'
    # the well-formed lines either side of the torn one are readable; the torn
    # one is skipped rather than merging with -- and swallowing -- the next append
    assert [e["seq"] for e in store.read_jsonl(target)] == [1, 3]


def test_append_line_after_torn_line_exceeding_text_buffer(tmp_path):
    """The text-mode write buffer is 8 KiB, so a torn line longer than that is a
    distinct code path from a short torn fragment -- the O(1) tail check (seek to
    the last byte) must still catch it regardless of the torn line's length."""
    target = tmp_path / "events" / "T-1.jsonl"
    _write_raw(target, b'{"seq": 1}\n')
    torn_payload = "x" * 9000  # > 8 KiB
    _write_raw(target, f'{{"seq": 2, "payload": "{torn_payload}'.encode())

    store.append_line(target, '{"seq": 3}')

    raw = target.read_bytes()
    assert raw.count(b"\n") == 3  # line 1, the now-terminated torn line, line 3
    assert [e["seq"] for e in store.read_jsonl(target)] == [1, 3]


def test_append_line_does_not_read_whole_file(tmp_path, monkeypatch):
    """The torn-tail check is a stat + a 1-byte read from the end -- never a full
    read of the target, so this stays O(1) in file size on every append."""
    target = tmp_path / "events" / "T-1.jsonl"
    store.append_line(target, '{"seq": 1}')
    store.append_line(target, '{"seq": 2}')

    real_open = target.__class__.open

    def _boom_read_text(self, *a, **k):
        raise AssertionError("append_line must not read the whole file")

    def _guarded_open(self, mode="r", *a, **k):
        if mode == "r":
            raise AssertionError("append_line must not open the target for a full text read")
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(target.__class__, "read_text", _boom_read_text)
    monkeypatch.setattr(target.__class__, "open", _guarded_open)
    store.append_line(target, '{"seq": 3}')
    monkeypatch.undo()

    assert [e["seq"] for e in store.read_jsonl(target)] == [1, 2, 3]


def test_inbox_append_command_survives_a_torn_tail(home):
    """append_line's other caller (inbox) is safe against the same failure mode."""
    inbox.append_command(home, "T-1", "ans", {"text": "ok"})
    path = store.inbox_path(home, "T-1")
    _write_raw(path, b'{"ts": "x", "command": "cmd", "args": {"text": "torn')  # crashed writer

    inbox.append_command(home, "T-1", "ans", {"text": "after-torn"})

    cmds = store.read_jsonl(path)
    assert [c["args"].get("text") for c in cmds] == ["ok", "after-torn"]
