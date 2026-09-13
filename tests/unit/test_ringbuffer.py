# tests/unit/test_ringbuffer.py
from __future__ import annotations

import random

import pytest

from pare_hardware_mcp.ringbuffer import CaptureBuffer, CursorError


def test_a_read_spanning_a_wrap_returns_contiguous_bytes_in_order():
    # capacity=10. First write fills the ring exactly with '0'..'9' at
    # physical positions 0..9 (offsets 0..9). Second write of 5 bytes wraps
    # back to physical position 0, overwriting offsets 0..4's physical slots
    # with the new bytes for offsets 10..14. The still-retained data (offsets
    # 5..14) is now split across two physical regions: positions 5..9 hold
    # the tail of the first write, positions 0..4 hold the second write. A
    # naive implementation that returns the physical buffer as one slice (or
    # returns the two halves in physical rather than logical order) would
    # produce "ABCDE56789" or the raw bytearray order instead of "56789ABCDE".
    buf = CaptureBuffer(capacity=10)
    buf.append(b"0123456789")
    buf.append(b"ABCDE")
    data, next_cursor, dropped, remaining = buf.read(5)
    assert data == b"56789ABCDE"
    assert dropped == 0
    assert next_cursor == 15
    assert remaining == 0


def test_falling_behind_by_more_than_capacity_returns_oldest_data_not_an_exception():
    # Two writes, both smaller than capacity individually, whose combined
    # total exceeds it -- distinct from a single oversized append (covered
    # separately below). A cursor left at 0 is now more than `capacity`
    # bytes behind head. This must come back with the oldest retained bytes
    # and dropped > 0 -- not raise, and not hand back an empty read.
    buf = CaptureBuffer(capacity=10)
    buf.append(b"0123456789")  # offsets 0..9
    buf.append(b"ABCDE")       # offsets 10..14; evicts offsets 0..4
    data, next_cursor, dropped, remaining = buf.read(0)
    assert dropped == 5
    assert data == b"56789ABCDE"
    assert next_cursor == 15
    assert remaining == 0


def test_dropped_is_exact_across_a_read_then_a_full_wraparound_write():
    # The brief's own worked recipe: write 100, read 50 (cursor now sits at
    # 50, 50 bytes still unread), write exactly `capacity` more (this alone
    # evicts every byte from the first write, read or not). dropped must be
    # precisely the 50 bytes that were written and never read -- not the
    # full 100, not an approximation, and not zero.
    buf = CaptureBuffer(capacity=1000)
    buf.append(bytes(range(100)) * 1)  # 100 distinct-ish bytes, offsets 0..99
    first, cursor, dropped0, _ = buf.read(0, limit=50)
    assert len(first) == 50 and dropped0 == 0 and cursor == 50

    buf.append(bytes((i % 256) for i in range(1000)))  # offsets 100..1099
    data, next_cursor, dropped, remaining = buf.read(cursor)
    assert dropped == 50
    assert next_cursor == 1100
    assert remaining == 0


def test_a_single_append_larger_than_capacity_retains_only_its_last_capacity_bytes():
    buf = CaptureBuffer(capacity=10)
    buf.append(bytes(range(25)))  # offsets 0..24, single call
    assert buf.head == 25
    data, next_cursor, dropped, remaining = buf.read(0)
    assert dropped == 15  # offsets 0..14 never survived this one call
    assert data == bytes(range(15, 25))
    assert next_cursor == 25
    assert remaining == 0


def test_a_limit_smaller_than_available_advances_the_cursor_by_bytes_returned_only():
    buf = CaptureBuffer(capacity=1000)
    buf.append(bytes(range(50)))
    data, next_cursor, dropped, remaining = buf.read(0, limit=10)
    assert data == bytes(range(10))
    assert next_cursor == 10  # not 50 -- only what was actually returned
    assert dropped == 0
    assert remaining == 40  # non-zero: there is more to come back for


def test_reading_at_head_is_a_cheap_quiet_line_not_an_error():
    buf = CaptureBuffer(capacity=1000)
    buf.append(b"hello")
    data, next_cursor, dropped, remaining = buf.read(buf.head)
    assert data == b""
    assert dropped == 0
    assert remaining == 0
    assert next_cursor == buf.head


def test_a_cursor_beyond_head_raises_rather_than_being_clamped():
    buf = CaptureBuffer(capacity=1000)
    buf.append(b"hello")
    with pytest.raises(CursorError):
        buf.read(buf.head + 1)


def test_bytes_returned_plus_dropped_equals_cursor_advance_over_random_sequences():
    # The identity that must hold for ANY sequence of appends and reads:
    # bytes_returned + dropped == next_cursor - cursor. Asserted as a
    # relationship, not a magic number, so it survives any legitimate
    # change to capacity, chunk size, or read pattern. Driven by a fixed
    # seed so failures reproduce.
    rng = random.Random(20260913)
    for _capacity in (1, 2, 7, 16, 137, 4096):
        buf = CaptureBuffer(capacity=_capacity)
        cursor = 0
        for _ in range(300):
            if rng.random() < 0.6:
                n = rng.randint(0, _capacity * 3)
                buf.append(rng.randbytes(n))
            else:
                limit = rng.choice([None, 0, 1, rng.randint(1, _capacity * 3)])
                data, next_cursor, dropped, remaining = buf.read(cursor, limit)
                assert len(data) + dropped == next_cursor - cursor
                assert remaining == buf.head - next_cursor
                cursor = next_cursor
