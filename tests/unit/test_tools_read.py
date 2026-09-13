# tests/unit/test_tools_read.py
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.ringbuffer import (CaptureBuffer, DEFAULT_READ_LIMIT,
                                          MAX_READ_LIMIT)


class FakeSession:
    id = "s-1"
    alive = True
    baud = 115200
    flow = "none"

    def __init__(self, capacity=1024):
        self.buffer = CaptureBuffer(capacity=capacity)
        self.gaps = []

    def gaps_overlapping(self, start_cursor, end_cursor):
        return [dict(g) for g in self.gaps
                if start_cursor <= g["at_cursor"] <= end_cursor]


class FakeManager:
    def __init__(self, capacity=1024):
        self.current = FakeSession(capacity=capacity)

    def get(self, session_id):
        if session_id != self.current.id:
            raise KeyError(session_id)
        return self.current


@pytest.fixture
def live(monkeypatch):
    """A session manager whose buffer is pre-loaded, with no real device."""
    mgr = FakeManager()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    return mgr


@pytest.fixture
def big(monkeypatch):
    """The same, with a buffer comfortably larger than MAX_READ_LIMIT.

    Sized from the constant rather than a literal so it stays larger than the
    ceiling if the ceiling ever moves.
    """
    mgr = FakeManager(capacity=4 * MAX_READ_LIMIT)
    monkeypatch.setattr(tools, "MANAGER", mgr)
    return mgr


async def test_read_returns_base64_not_raw_text(live):
    live.current.buffer.append(b"\x1b[2Kboot\xff\xfe")
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    # Raw target bytes must never travel as text: they are untrusted, they reach
    # the operator's approval prompt, and they are not valid UTF-8 in general.
    assert base64.b64decode(out["data_b64"]) == b"\x1b[2Kboot\xff\xfe"
    assert "data" not in out


async def test_read_reports_dropped_and_remaining(live):
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["dropped"] == 0
    assert out["remaining"] == 0
    assert out["next_cursor"] == 0


async def test_a_limited_read_leaves_remaining_nonzero(live):
    live.current.buffer.append(b"0123456789")
    out = json.loads(await tools.console_read(session="s-1", cursor=0, limit=4))
    assert base64.b64decode(out["data_b64"]) == b"0123"
    assert out["next_cursor"] == 4
    assert out["remaining"] == 6


async def test_reading_an_unknown_session_is_an_error_naming_it(live):
    out = json.loads(await tools.console_read(session="nope", cursor=0))
    assert out["error"]
    assert "nope" in out["error"]


async def test_read_reports_no_capture_gaps_when_none_were_recorded(live):
    live.current.buffer.append(b"abc")
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["capture_gaps"] == []


async def test_read_flags_a_capture_gap_whose_cursor_falls_inside_the_window(live):
    # A suspension leaves no byte range in cursor space -- capture just stops
    # advancing `head` and resumes from the same offset -- so `dropped` alone
    # cannot report it. A read spanning across the recorded cursor must.
    live.current.buffer.append(b"before")
    live.current.gaps.append({"at_cursor": 6, "duration_s": 12.0, "reason": "baud scan"})
    live.current.buffer.append(b"after")

    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["dropped"] == 0, "no bytes were evicted -- the hole is temporal, not spatial"
    assert out["capture_gaps"] == [{"at_cursor": 6, "duration_s": 12.0, "reason": "baud scan"}]


async def test_read_does_not_flag_a_gap_outside_the_returned_window(live):
    live.current.buffer.append(b"0123456789")
    live.current.gaps.append({"at_cursor": 6, "duration_s": 12.0, "reason": "baud scan"})

    # A read that never reaches offset 6 must not report a gap that lies past it.
    out = json.loads(await tools.console_read(session="s-1", cursor=0, limit=3))
    assert out["next_cursor"] == 3
    assert out["capture_gaps"] == []

    # A read that starts after the gap must not report it either.
    out = json.loads(await tools.console_read(session="s-1", cursor=7))
    assert out["capture_gaps"] == []
    # Neither half above distinguishes the shipped window expression from
    # `(cursor, next_cursor)` -- nothing was dropped, so the two are equal.
    # The test below is the one that does.


async def test_read_does_not_flag_a_gap_in_the_bytes_it_could_not_return(live):
    """The window starts where the DATA starts, not where the caller asked.

    `gaps_overlapping(next_cursor - len(data), next_cursor)` and
    `gaps_overlapping(cursor, next_cursor)` are identical whenever
    `dropped == 0`, which is every other gap test in this file -- so the
    mutation survived the whole suite. They diverge exactly when the caller
    fell behind and the ring wrapped: `cursor` then points into the evicted
    region, and the mutated expression reports gaps sitting among bytes the
    caller never received and cannot receive. A language model told "there was
    a 12-second hole in what you just read" about bytes that are not in its
    hands reasons about a boot log it does not have.

    Pre-existing, but BL1's default limit makes short reads the norm rather
    than the exception, so the expression is materially more load-bearing than
    when it was written.

    Would catch `(cursor, next_cursor)` and `(0, next_cursor)`.
    """
    capacity = live.current.buffer.capacity          # 1024
    live.current.buffer.append(b"x" * (capacity + 476))
    # head = 1500, oldest retained = 476: offsets 0..475 are gone for good.
    gap_in_the_evicted_region = {"at_cursor": 100, "duration_s": 12.0,
                                 "reason": "baud scan"}
    live.current.gaps.append(gap_in_the_evicted_region)

    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["dropped"] == 476, "the fixture must actually evict"
    assert out["next_cursor"] - len(base64.b64decode(out["data_b64"])) == 476
    assert out["capture_gaps"] == [], \
        "a gap among the dropped bytes is not a gap in what was returned"

    # ...and the same gap IS reported once a read's window covers it. Asserted
    # as the pair so the test cannot pass by never reporting anything.
    live.current.gaps.append({"at_cursor": 600, "duration_s": 3.0,
                              "reason": "baud scan"})
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert [g["at_cursor"] for g in out["capture_gaps"]] == [600]


async def test_read_with_an_invalid_cursor_returns_an_error_not_an_exception(live):
    # A cursor ahead of `head` (or negative) never came from this API and can
    # never be the ordinary "fell behind, some bytes were dropped" case --
    # `CaptureBuffer.read` raises `CursorError` for it. The handler must catch
    # that and return the error contract: an exception escaping a tool handler
    # becomes an opaque transport-level failure instead of something the model
    # can read and correct.
    live.current.buffer.append(b"abc")  # head is now 3
    out = json.loads(await tools.console_read(session="s-1", cursor=99))
    assert out["error"]
    assert "99" in out["error"]


async def test_console_status_serialises_none_rts_as_json_null(monkeypatch):
    # Under flow="rtscts" pyserial never applies a requested RTS, so the
    # session reports rts=None rather than claiming a value it did not set.
    # console_status must pass that through as JSON null, not coerce it to
    # false -- that would be the false claim None exists to avoid.
    class FakeManager:
        def status(self):
            return {"open": True, "session": "s-1", "rts": None}

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    raw = await tools.console_status()
    assert '"rts": null' in raw
    out = json.loads(raw)
    assert out["rts"] is None


# --------------------------------------------------------------------------
# The payload bound. Before this, `limit` had no default and no ceiling: one
# unbounded console_read against a filled 64 MiB buffer measured 89.5 MB of
# JSON in 514 ms. `remaining`/`next_cursor` are what make a bound safe --
# draining across several calls is the designed path.
# --------------------------------------------------------------------------

async def test_a_default_read_is_bounded_not_the_whole_buffer(big):
    """Catches `limit: int | None = None` reaching the buffer as None.

    A handler that passes the caller's absent limit straight through returns
    everything, which is exactly the pre-fix behaviour.
    """
    big.current.buffer.append(b"x" * (3 * MAX_READ_LIMIT))
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    data = base64.b64decode(out["data_b64"])
    assert len(data) == DEFAULT_READ_LIMIT
    assert out["limit_applied"] == DEFAULT_READ_LIMIT
    assert out["remaining"] == 3 * MAX_READ_LIMIT - DEFAULT_READ_LIMIT


async def test_a_limit_above_the_ceiling_is_clamped(big):
    """Catches a default with no clamp: a caller can still ask for everything."""
    big.current.buffer.append(b"y" * (3 * MAX_READ_LIMIT))
    out = json.loads(await tools.console_read(
        session="s-1", cursor=0, limit=10_000_000))
    data = base64.b64decode(out["data_b64"])
    assert len(data) == MAX_READ_LIMIT
    assert out["limit_applied"] == MAX_READ_LIMIT
    assert out["remaining"] > 0


async def test_a_limit_under_the_ceiling_is_honoured_exactly(big):
    """Catches a clamp written as a constant rather than a min(): a handler
    that always returns MAX_READ_LIMIT would pass the test above and fail
    here."""
    big.current.buffer.append(b"z" * (3 * MAX_READ_LIMIT))
    out = json.loads(await tools.console_read(session="s-1", cursor=0, limit=10))
    assert len(base64.b64decode(out["data_b64"])) == 10
    assert out["limit_applied"] == 10


async def test_a_bounded_read_still_drains_completely_via_next_cursor(big):
    """The bound must not cost the caller any bytes.

    Asserted as a relationship -- every byte appended comes back, in order,
    across however many calls `remaining` says are needed -- rather than
    against a call count, which would break on the next legitimate change to
    either constant. Catches a clamp that advances `next_cursor` past the
    bytes it did not return.
    """
    payload = bytes(range(256)) * ((DEFAULT_READ_LIMIT * 3) // 256)
    big.current.buffer.append(payload)

    collected = bytearray()
    cursor = 0
    for _ in range(1000):                       # bounded so a bug cannot hang
        out = json.loads(await tools.console_read(session="s-1", cursor=cursor))
        collected.extend(base64.b64decode(out["data_b64"]))
        cursor = out["next_cursor"]
        assert out["dropped"] == 0
        if out["remaining"] == 0:
            break
    else:
        pytest.fail("remaining never reached 0")
    assert bytes(collected) == payload


async def test_a_non_integer_limit_is_an_error_not_a_transport_failure(live):
    """Catches `min(limit, ...)` on a non-int: TypeError is not caught by the
    handler's `except KeyError`/`except CursorError`, so it escapes the
    {"error": ...} contract entirely.

    This pins the HANDLER's contract, not the wire's. FastMCP's pydantic model
    validates `limit` before dispatch (verified against `build_server()
    .call_tool`: it coerces "4096"/4096.0/True to an int and rejects 4096.5
    itself), so the wire cannot deliver a string here today -- see
    `_read_limit`'s docstring.
    """
    live.current.buffer.append(b"hello")
    out = json.loads(await tools.console_read(
        session="s-1", cursor=0, limit="4096"))
    assert out["error"]
    assert "limit" in out["error"]


async def test_a_negative_limit_is_normalised_to_zero_and_reported_as_zero(live):
    """Catches `max(0, ...)` being dropped from `_read_limit`.

    The returned BYTES are not the discriminator here -- `CaptureBuffer.read`
    has its own `max(0, ...)` and would clamp a negative to an empty read
    either way -- so asserting only on `data_b64` would be a test that passes
    against both implementations. `limit_applied` is produced by `_read_limit`
    itself and is what actually distinguishes them: -5 in, 0 reported.
    """
    live.current.buffer.append(b"0123456789")
    out = json.loads(await tools.console_read(
        session="s-1", cursor=0, limit=-5))
    assert base64.b64decode(out["data_b64"]) == b""
    assert out["limit_applied"] == 0
    assert out["remaining"] == 10
