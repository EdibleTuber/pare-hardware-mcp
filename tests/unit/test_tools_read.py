# tests/unit/test_tools_read.py
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools


@pytest.fixture
def live(monkeypatch):
    """A session manager whose buffer is pre-loaded, with no real device."""
    from pare_hardware_mcp.ringbuffer import CaptureBuffer

    class FakeSession:
        id = "s-1"
        alive = True
        baud = 115200
        flow = "none"
        def __init__(self):
            self.buffer = CaptureBuffer(capacity=1024)
            self.gaps = []
        def gaps_overlapping(self, start_cursor, end_cursor):
            return [dict(g) for g in self.gaps
                    if start_cursor <= g["at_cursor"] <= end_cursor]

    class FakeManager:
        def __init__(self):
            self.current = FakeSession()
        def get(self, session_id):
            if session_id != self.current.id:
                raise KeyError(session_id)
            return self.current

    mgr = FakeManager()
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
