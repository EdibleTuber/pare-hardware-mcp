"""Fixed-capacity ring buffer holding everything captured from a target board.

Capture starts the instant the serial port opens, before any reader has
asked for data -- the boot log is the single most valuable artifact a
hardware investigation gets, and it arrives before a question can be asked.
A reader may fall behind arbitrarily (an hour of silence unread is ~40 MB at
115200 baud), so once the buffer wraps, old bytes are evicted. When that
happens, `read()` reports exactly how many bytes were lost rather than
returning a shorter or stale read silently: a boot log with an untold hole
is worse than an explicit error, because the consumer is a language model
that will reason confidently straight across the gap and reach a wrong
conclusion about the hardware.

Cursors are absolute byte offsets since the session opened, never ring
indices -- a cursor stays meaningful after any number of wraps, and a
caller can hold one across many `read()` calls without knowing anything
about `capacity`.

No locking here. A later task adds a background reader thread that calls
`.append()` while tool-call handlers call `.read()` concurrently; every
mutation in this module is a short, non-yielding sequence of plain
attribute and bytearray-slice operations (no I/O, no awaiting) so that a
lock can be dropped around each call without redesigning anything -- but
this module does not add that lock itself, and as written it is not safe
to call from more than one thread at once.
"""
from __future__ import annotations

# At 115200 baud, 8N1, each byte on the wire costs 10 bits (1 start + 8 data
# + 1 stop): 115200 / 10 = 11520 B/s ~= 11.25 KiB/s. 64 MiB (67 108 864 B) /
# 11520 B/s ~= 5825 s ~= 97 minutes of continuous full-rate capture before
# the oldest byte is evicted. A real boot log runs 15-60s (170 KB-675 KB)
# except a panic loop, which can run to several MB -- so this is generous
# headroom, not a tight fit, and an hour of an unread quiet line (~40 MB) is
# comfortably inside it. The Pi host has ~7.4 GiB of memory and runs at most
# one session at a time, so oversizing this constant is cheap and
# undersizing is the only real risk. A future faster baud rate should
# rederive from (baud / 10) bytes/sec.
DEFAULT_CAPACITY = 64 * 1024 * 1024  # 64 MiB


class CursorError(ValueError):
    """A cursor ahead of `head` was requested.

    That is a caller bug (asking for bytes not yet written), not the normal
    "fell behind and some bytes were dropped" case -- which is not an error
    at all, see `CaptureBuffer.read`.
    """


class CaptureBuffer:
    """A fixed-capacity ring buffer over everything captured from a target.

    `head` is the total number of bytes ever appended -- also the cursor a
    reader gets if it opens now and wants only what comes next. `append`
    writes; `read(cursor, limit)` returns the bytes from the absolute
    offset `cursor` onward, evicting the oldest data once more than
    `capacity` bytes total have been written.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self.capacity = capacity
        self._buf = bytearray(capacity)
        self.head = 0

    def append(self, data: bytes) -> None:
        """Append `data`, advancing `head` by `len(data)`.

        Legal even when `len(data) > capacity`: every byte of `data` before
        its last `capacity` bytes is overwritten by a later byte of this
        same call before anything could ever read it, so only that tail is
        physically written. `head` still advances by the full length, so a
        cursor issued before this call correctly sees the rest as dropped.
        """
        n = len(data)
        if n == 0:
            return
        if n >= self.capacity:
            # Keep only the tail that survives; place it at the physical
            # position its true absolute offsets (head+n-capacity ..
            # head+n-1) map to, so `read`'s `offset % capacity` finds it.
            tail_offset = self.head + (n - self.capacity)
            data = data[n - self.capacity:]
            start = tail_offset % self.capacity
        else:
            start = self.head % self.capacity
        self._write_at(start, data)
        self.head += n

    def _write_at(self, start: int, data: bytes) -> None:
        n = len(data)
        end = start + n
        if end <= self.capacity:
            self._buf[start:end] = data
        else:
            first = self.capacity - start
            self._buf[start:] = data[:first]
            self._buf[: end - self.capacity] = data[first:]

    def _read_at(self, start: int, n: int) -> bytes:
        end = start + n
        if end <= self.capacity:
            return bytes(self._buf[start:end])
        first = self.capacity - start
        return bytes(self._buf[start:]) + bytes(self._buf[: end - self.capacity])

    def read(self, cursor: int, limit: int | None = None) -> tuple[bytes, int, int, int]:
        """Return `(data, next_cursor, dropped, remaining)` from `cursor`.

        - A `cursor` ahead of `head` raises `CursorError` -- that data has
          not been written yet, which is a caller bug.
        - A `cursor` behind the oldest byte still retained is the normal
          "fell behind" case, not an error: `dropped` reports exactly how
          many bytes between `cursor` and the oldest retained byte were
          evicted by wraparound, and the read proceeds from that oldest
          byte, returning it rather than raising or coming back empty.
        - `limit` bounds how many bytes come back from *this* call, not how
          far the cursor is allowed to advance: `next_cursor` reflects only
          the bytes actually returned (after `dropped` bytes, if any, were
          skipped), so calling again with `next_cursor` picks up the rest.
        - `remaining` is `head - next_cursor`: what is still unread after
          this call, letting a caller drain without guessing how many more
          calls it needs.
        """
        if cursor > self.head:
            raise CursorError(
                f"cursor {cursor} is ahead of head {self.head} -- "
                "that data has not been written yet"
            )

        oldest_retained = max(0, self.head - self.capacity)
        dropped = max(0, oldest_retained - cursor)
        start = max(cursor, oldest_retained)
        available = self.head - start
        to_return = available if limit is None else max(0, min(limit, available))

        data = self._read_at(start % self.capacity, to_return)
        next_cursor = start + to_return
        remaining = self.head - next_cursor
        return data, next_cursor, dropped, remaining
