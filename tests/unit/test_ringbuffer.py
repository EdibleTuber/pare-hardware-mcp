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


def test_a_negative_cursor_raises_rather_than_inflating_dropped():
    # `head` and every `next_cursor` this buffer has ever returned are >= 0
    # by construction, so a negative cursor only ever reaches `read` via a
    # caller bug -- symmetric with a cursor ahead of `head`, which already
    # raises. Concretely, in this scenario head=15 and oldest_retained=5,
    # so read(0) correctly reports dropped=5 (the real offsets 0..4). Before
    # this check existed, `dropped = max(0, oldest_retained - cursor)` had
    # no floor at cursor=0, so read(-5) reported dropped=10: five phantom
    # bytes for offsets -5..-1, which were never written, stacked on top of
    # the five real ones.
    buf = CaptureBuffer(capacity=10)
    buf.append(b"0123456789")
    buf.append(b"ABCDE")
    with pytest.raises(CursorError):
        buf.read(-5)


def test_read_matches_an_independent_reference_model_over_random_sequences():
    # The property test this replaces asserted
    # `len(data) + dropped == next_cursor - cursor`, which turned out to be
    # a tautology: given the implementation's own `dropped = start - cursor`
    # and `next_cursor = start + to_return`, that identity is algebra on
    # `start` and holds for ANY value `start` takes -- including a
    # hypothetical bug where `oldest_retained` were hardcoded to 0 and
    # `dropped` were always 0, silently never reporting an eviction. It
    # tested the return tuple's internal self-consistency, not whether
    # `dropped` or the returned bytes are actually correct.
    #
    # This version checks against an independent oracle: a plain,
    # ever-growing bytearray that records every byte ever appended and
    # never evicts anything, so it cannot share the ring's wraparound
    # arithmetic -- or any bug in it -- with the code under test. Ground
    # truth for what a fixed-capacity ring can still be holding is derived
    # from that oracle's own length and the known `capacity`, not from any
    # of `CaptureBuffer`'s attributes or helper methods.
    rng = random.Random(20260913)
    for capacity in (1, 2, 7, 16, 137, 4096):
        buf = CaptureBuffer(capacity=capacity)
        reference = bytearray()  # everything ever appended, absolute-offset indexed
        cursor = 0
        for _ in range(300):
            if rng.random() < 0.6:
                chunk = rng.randbytes(rng.randint(0, capacity * 3))
                buf.append(chunk)
                reference.extend(chunk)
            else:
                limit = rng.choice([None, 0, 1, rng.randint(1, capacity * 3)])
                data, next_cursor, dropped, remaining = buf.read(cursor, limit)

                # The offset the returned bytes claim to start at, per the
                # buffer's own report of how much it dropped -- checked
                # against the oracle's independently-held content, not
                # against anything the buffer computed internally.
                start = cursor + dropped
                assert bytes(reference[start:start + len(data)]) == data

                # Ground truth for `dropped`: everything the oracle has
                # ever seen "existed"; a `capacity`-sized ring can only
                # ever retain the last `capacity` of those bytes, which is
                # a fact about fixed-size ring buffers, not a number pulled
                # from this implementation.
                oldest_survivor = max(0, len(reference) - capacity)
                expected_dropped = max(0, oldest_survivor - cursor)
                assert dropped == expected_dropped

                assert buf.head == len(reference)
                assert remaining == len(reference) - next_cursor
                cursor = next_cursor
