# tests/unit/test_tools_concurrency.py
"""Handlers must not hold the event loop, and must survive each other.

Two halves of one property, which is why they share a file. A blocking call
inside an `async def` stops every other tool call on this worker; moving it to
a thread fixes that and, in the same stroke, makes handler interleavings
reachable that the event loop used to prevent. A handler that escapes the
{"error": ...} contract under one of those interleavings is worse than the
stall it replaced -- the model gets an opaque transport failure exactly when
it most needs to read what happened.

The discriminator here is the TICK COUNT of a probe task running alongside the
call. With a blocking handler the probe gets no control at all and ticks zero
times; with the work on a thread it ticks throughout. It is asserted as a
floor well below the expected count, not as an exact number, so a slow box
cannot turn it into a flake while a re-inlined call still fails it hard.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.config import Config
from pare_hardware_mcp.session import SessionError

# How long each fake blocking call holds its thread, and how often the probe
# tries to run. The ratio is what matters: ~40 ticks are due in that window,
# and the floor below is a quarter of that.
BLOCK_S = 0.4
TICK_S = 0.01
MIN_TICKS = 10


async def ticks_during(call):
    """Run `call`, counting how many times the event loop got control.

    Returns `(result, ticks)`. A handler that blocks the loop yields zero:
    awaiting a coroutine that never awaits anything does not give the
    scheduler an opportunity to run the probe at all.
    """
    done = asyncio.Event()
    counter = {"n": 0}

    async def probe():
        while not done.is_set():
            counter["n"] += 1
            await asyncio.sleep(TICK_S)

    task = asyncio.create_task(probe())
    try:
        result = await call
    finally:
        done.set()
        await task
    return result, counter["n"]


# --------------------------------------------------------------------------
# Nothing blocking on the event loop.
# --------------------------------------------------------------------------

async def test_console_send_does_not_hold_the_event_loop(monkeypatch):
    """`ConsoleSession.write` waits on `_write_lock` with no timeout, and
    `scan_baud` holds that lock for a whole scan. Called inline it stalls the
    worker; measured over a real pty at 3.78s with a 50 ms status poller
    completing 0 of the ~75 polls due.

    Would catch `sess.write(payload)` being called directly again.
    """
    class FakeSession:
        id, alive = "s-1", True
        def write(self, data):
            time.sleep(BLOCK_S)

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    raw, ticks = await ticks_during(tools.console_send(
        session="s-1", data_b64=base64.b64encode(b"hi").decode()))
    assert json.loads(raw)["sent"] == 2
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"


async def test_console_close_does_not_hold_the_event_loop(monkeypatch):
    """`_shutdown` joins the reader (JOIN_TIMEOUT 2.0s) and then waits on
    `_write_lock` (WRITE_DRAIN_TIMEOUT 6.0s) because the fd must not close
    under an in-flight write -- up to 8s of dead worker if run inline.

    Would catch `MANAGER.close(session)` being called directly again.
    """
    class FakeManager:
        def close(self, sid):
            if sid != "s-1":
                raise KeyError(sid)
            time.sleep(BLOCK_S)

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    raw, ticks = await ticks_during(tools.console_close(session="s-1"))
    assert json.loads(raw)["closed"] == "s-1"
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"


async def test_console_open_does_not_hold_the_event_loop(monkeypatch):
    """Allocates the 64 MiB capture buffer, opens a tty, starts the reader,
    and can queue on the manager lock behind another open."""
    class FakeSession:
        id = "s-1"
        class device:
            by_id, serial = "/dev/x", "SN-9"
        baud, flow, dtr, rts = 115200, "none", False, False
        class buffer:
            head = 0

    class FakeManager:
        def open(self, **kwargs):
            time.sleep(BLOCK_S)
            return FakeSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial=None))
    raw, ticks = await ticks_during(tools.console_open())
    assert json.loads(raw)["session"] == "s-1"
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"


async def test_bench_status_does_not_hold_the_event_loop_on_a_wedged_root(monkeypatch):
    """bench_status stats an operator-declared artifact root -- in practice a
    removable drive. A stat on a wedged USB filesystem blocks in the kernel,
    and this is the tool an operator reaches for precisely when the bench is
    misbehaving."""
    def slow_root(root):
        time.sleep(BLOCK_S)
        return {"declared": root, "present": True, "writable": True,
                "drive_id": None}

    monkeypatch.setattr(tools, "_artifact_root_status", slow_root)
    monkeypatch.setattr(tools, "list_serial_devices", lambda *a, **k: [])
    raw, ticks = await ticks_during(tools.bench_status())
    assert json.loads(raw)["artifact_root"]["present"] is True
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"


async def test_list_devices_does_not_hold_the_event_loop(monkeypatch):
    def slow_scan(*a, **k):
        time.sleep(BLOCK_S)
        return []

    monkeypatch.setattr(tools, "list_serial_devices", slow_scan)
    raw, ticks = await ticks_during(tools.list_devices())
    assert json.loads(raw)["devices"] == []
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"


@pytest.mark.parametrize("handler, expect_key", [
    (lambda: tools.console_status(), "open"),
    (lambda: tools.console_read(session="nope"), "error"),
    (lambda: tools.console_send(session="nope", data_b64="aGk="), "error"),
    (lambda: tools.console_detect_baud(), "error"),
    (lambda: tools.bench_status(), "session"),
])
async def test_every_handler_keeps_the_loop_alive_while_an_open_holds_the_lock(
        monkeypatch, handler, expect_key):
    """The reason EVERY manager call goes to a thread, not just the slow ones.

    `status()`, `get()` and `current` are each microseconds of work -- but
    each takes the manager `_lock`, and `SessionManager.open` holds that lock
    across `resolve_device`, the `open(2)` on the tty, the 64 MiB buffer
    allocation and the reader thread's start (session.py:875-920). Once
    `console_open` itself runs on a thread, any of these on the event loop
    can be parked on that lock for the whole of an open. Whether a call is
    safe inline is a property of who else takes its lock, which no single
    call site can check -- hence the flat rule, and hence this test covering
    every handler that touches the manager rather than one representative.

    The parametrised calls deliberately name a session that does not exist:
    each one still has to acquire `_lock` before it can find that out, which
    is the whole point. Uses a REAL SessionManager so the lock is the real
    one; only the port open itself is faked.

    Would catch any of `MANAGER.status()`, `MANAGER.get(...)` or
    `MANAGER.current` being read directly on the loop again.
    """
    from pare_hardware_mcp import session as session_mod
    from pare_hardware_mcp.devices import ResolvedDevice
    from pare_hardware_mcp.session import SessionManager

    monkeypatch.setattr(tools, "MANAGER", SessionManager(capacity=1024))
    monkeypatch.setattr(tools, "CONFIG",
                        Config(device="/dev/x", expect_serial=None))
    monkeypatch.setattr(
        session_mod, "resolve_device",
        lambda path, *, expect_serial: ResolvedDevice(
            by_id=path, tty="/dev/null", serial=None, interface="if00"))

    def slow_open_port(self, resolved, **kwargs):
        time.sleep(BLOCK_S)
        raise SessionError("port refused")

    monkeypatch.setattr(SessionManager, "_open_port", slow_open_port)

    opening = asyncio.create_task(tools.console_open())
    await asyncio.sleep(0.02)          # let the open take the manager lock

    raw, ticks = await ticks_during(handler())
    assert expect_key in json.loads(raw)
    assert ticks >= MIN_TICKS, f"the loop was starved: {ticks} ticks"

    assert json.loads(await opening)["error"]


# --------------------------------------------------------------------------
# ...and the interleavings that moving off the loop makes reachable.
# --------------------------------------------------------------------------

async def test_read_on_a_session_closed_underneath_it_keeps_the_error_contract(monkeypatch):
    """The prerequisite for the change above.

    `console_read` calls `MANAGER.get(session)` and then reads `sess.buffer`
    with no await between them, and `ConsoleSession.buffer` raises
    `SessionError` once `_shutdown` has set `_buffer = None`
    (session.py:186-193). `console_read` caught only `KeyError` and
    `CursorError`. While close ran on the event loop that sequence could not
    interleave; with close on a thread it can, and the SessionError would
    leave the tool as an opaque transport failure.

    The fake reproduces the post-close state exactly -- the property raising
    -- rather than racing a real close, so it is a deterministic test of the
    handler's contract and not of the scheduler. Would catch the
    `except SessionError` clause being dropped: the await raises instead of
    returning JSON.
    """
    class ClosedSession:
        id, alive = "s-1", False
        @property
        def buffer(self):
            raise SessionError(
                "session s-1 is closed and its capture was discarded")
        def gaps_overlapping(self, a, b): return []

    class FakeManager:
        def __init__(self): self.current = ClosedSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_read(session="s-1", cursor=0))
    assert out["error"]
    assert "discarded" in out["error"]


async def test_open_reports_the_cursor_capture_had_already_reached(monkeypatch):
    """`cursor` is not decorative and is not always 0.

    Capture starts inside `SessionManager.open` -- the reader thread is
    running before it returns -- so by the time a response is built the
    buffer can already hold the first bytes of a boot log. `cursor` is the
    offset a reader must start from to get everything after that point, and a
    wrong one silently re-reads or skips the start of the capture.

    Every other fake in this suite has `head = 0`, which is why mutating
    `cursor=sess.buffer.head` to `cursor=0` survived the whole suite: the one
    line carrying round 2's Critical was untested in both respects. This is
    the other respect.
    """
    class FakeSession:
        id = "s-1"
        class device:
            by_id, serial = "/dev/x", "SN-9"
        baud, flow, dtr, rts = 115200, "none", False, False
        class buffer:
            head = 4096

    class FakeManager:
        def open(self, **kwargs): return FakeSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial=None))
    out = json.loads(await tools.console_open())
    assert out["cursor"] == 4096


async def test_open_racing_a_close_keeps_the_error_contract(monkeypatch):
    """Round 2's Critical, and it was introduced by round 1's own fix.

    `cursor=sess.buffer.head` sat OUTSIDE console_open's `except Exception`,
    and `ConsoleSession.buffer` raises `SessionError` once `_shutdown` has set
    `_buffer = None` (session.py:188-193, :806). Before `MANAGER.open` moved
    to a thread there was no await between the open and that line, so it was
    unreachable; adding the await is what made it reachable.
    `SessionManager.open` publishes `_current` under the manager lock
    (session.py:951) BEFORE the thread returns, so a concurrent
    console_status can reveal the new id and a console_close on it can land
    in the window.

    The fake reproduces the post-`_shutdown` state exactly -- the property
    raising -- rather than racing a real close, so this tests the handler's
    contract and not the scheduler.

    Would catch (and does catch, verified against 89d63fa) the read being
    taken on the event loop outside the try: the await raises `SessionError`
    straight out of the tool instead of returning JSON.
    """
    class ClosedUnderneathSession:
        id = "s-2"
        class device:
            by_id, serial = "/dev/x", "SN-9"
        baud, flow, dtr, rts = 115200, "none", False, False

        @property
        def buffer(self):
            raise SessionError(
                "session s-2 is closed and its capture was discarded")

    class FakeManager:
        def open(self, **kwargs): return ClosedUnderneathSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial=None))

    out = json.loads(await tools.console_open())
    # Not an error: the port really was opened, and every other field is
    # still true. What is gone is the capture.
    assert "error" not in out
    assert out["session"] == "s-2"
    assert out["cursor"] is None
    # ...and the null is explained rather than left to be inferred.
    assert "console_status" in out["note"]


async def test_a_send_racing_a_close_keeps_the_error_contract(monkeypatch):
    """`write` refuses inside `_write_lock` once `_closing` is set. That
    refusal is a `SessionError` and must reach the caller as JSON, not as a
    raise -- unchanged by the move to a thread, and worth pinning because the
    move is what makes the race real."""
    class FakeSession:
        id, alive = "s-1", True
        def write(self, data):
            raise SessionError("session s-1 is closing or closed; nothing "
                               "was written")

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="aGk="))
    assert out["error"]
    assert "nothing was written" in out["error"]
    assert "sent" not in out


async def test_concurrent_handlers_all_return_the_error_contract(monkeypatch):
    """A read, a send and a close dispatched together against one session
    that is going away. Every one of them must come back as JSON -- an
    exception out of any handler is the failure this whole file is about."""
    class DyingSession:
        id, alive, death_reason = "s-1", False, "closing"
        @property
        def buffer(self):
            raise SessionError("session s-1 is closed and its capture was "
                               "discarded")
        def gaps_overlapping(self, a, b): return []
        def write(self, data):
            raise SessionError("session s-1 is closing or closed")

    class FakeManager:
        def __init__(self): self.current = DyingSession()
        def get(self, sid): return self.current
        def close(self, sid): raise KeyError(sid)

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    raws = await asyncio.gather(
        tools.console_read(session="s-1", cursor=0),
        tools.console_send(session="s-1", data_b64="aGk="),
        tools.console_close(session="s-1"),
    )
    for raw in raws:
        assert json.loads(raw)["error"], raw
