# tests/unit/test_session.py
"""Session lifecycle against a pty standing in for the target board.

`os.openpty()` gives two ends. The slave is the "device": a by-id symlink in
a tmpdir points at it, so `resolve_device` sees the same shape it sees on the
bench. The master is the test playing the target board -- what it writes is
what the console captures, and closing it is the unplug.

A pty is not a UART. It has no modem-control lines (TIOCMGET returns ENOTTY),
so these tests cannot prove DTR reached a wire; they prove the value reached
the port object. Step 5 of the brief -- a real Tigard -- is what checks the
wire.
"""
from __future__ import annotations

import gc
import inspect
import os
import re
import select
import threading
import time

import pytest
import serial

from pare_hardware_mcp.baud import MAX_BAUD_RATE
from pare_hardware_mcp.devices import DeviceError
from pare_hardware_mcp.ringbuffer import CaptureBuffer
from pare_hardware_mcp.session import (WRITE_DRAIN_TIMEOUT, ConsoleSession,
                                        SessionError, SessionManager)

TIGARD = "usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0"
SERIAL = "TG1119e7"

# Generous: every use is "wait until the reader thread has noticed", and the
# reader's poll interval is tens of milliseconds. A slow CI box should not
# turn a correctness test into a flake, and a broken implementation fails by
# never satisfying the predicate, not by being slow.
DEADLINE = 5.0


def until(predicate, timeout: float = DEADLINE, what: str = "condition"):
    """Poll `predicate` until true; fail the test with `what` if it never is."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


class Pty:
    """A by-id symlink pointing at a pty slave, plus the master end."""

    def __init__(self, by_id: str, master: int, link):
        self.by_id = by_id
        self.master = master
        self._link = link

    def send(self, data: bytes) -> None:
        """Play the target board: put bytes on the line."""
        os.write(self.master, data)

    def recv(self, timeout: float = 0.5) -> bytes:
        """Read what the target would see -- never blocking the test forever."""
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return b""
        return os.read(self.master, 65536)

    def drain_slowly(self, stop: threading.Event, chunk: int = 512,
                     interval: float = 0.002) -> threading.Thread:
        """Read the far end slowly, so a big write stays in flight.

        Returns the thread; the bytes it collects land in `self.received`.
        """
        self.received = bytearray()
        os.set_blocking(self.master, False)

        def pump():
            while not stop.is_set():
                try:
                    self.received.extend(os.read(self.master, chunk))
                except BlockingIOError:
                    pass
                except OSError:
                    return
                time.sleep(interval)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        return thread

    def drain_rest(self) -> None:
        """Collect whatever is still sitting in the pty buffer."""
        os.set_blocking(self.master, False)
        while True:
            try:
                self.received.extend(os.read(self.master, 65536))
            except (BlockingIOError, OSError):
                return

    def unplug_far_end(self) -> None:
        """Close the master: the device node stops answering reads."""
        if self.master is not None:
            os.close(self.master)
            self.master = None

    def unlink_by_id(self) -> None:
        """Remove the by-id symlink: what udev does on a USB unplug."""
        self._link.unlink()


def _make_pty(tmp_path, name: str = TIGARD) -> Pty:
    master, slave = os.openpty()
    tty = os.ttyname(slave)
    os.close(slave)  # the master keeps the pts node alive
    root = tmp_path / "by-id"
    root.mkdir(exist_ok=True)
    link = root / name
    link.symlink_to(tty)
    return Pty(str(link), master, link)


@pytest.fixture
def pty(tmp_path):
    dev = _make_pty(tmp_path)
    yield dev
    if dev.master is not None:
        try:
            os.close(dev.master)
        except OSError:
            pass


@pytest.fixture
def second_pty(tmp_path):
    dev = _make_pty(tmp_path, "usb-SecuringHardware.com_Tigard_V1.1_TG2222bb-if01-port0")
    yield dev
    if dev.master is not None:
        try:
            os.close(dev.master)
        except OSError:
            pass


@pytest.fixture
def manager():
    m = SessionManager()
    yield m
    # No test may leak a reader thread or an fd into the next one.
    cur = m.current
    if cur is not None:
        m.close(cur.id)


def open_ok(manager, pty, **kw) -> ConsoleSession:
    kw.setdefault("expect_serial", None)
    kw.setdefault("baud", 115200)
    kw.setdefault("dtr", False)
    kw.setdefault("rts", False)
    return manager.open(device=pty.by_id, **kw)


def can_open_exclusively(path: str) -> bool:
    """True if nobody else holds the port -- the check that `open` did not grab it."""
    port = serial.Serial()
    port.port = path
    port.exclusive = True
    port.timeout = 0.05
    try:
        port.open()
    except serial.SerialException:
        return False
    port.close()
    return True


# --------------------------------------------------------------------------
# Invariant 2: capture starts inside open(), before it returns.
# --------------------------------------------------------------------------

def test_bytes_sent_before_any_read_call_are_in_the_first_read(manager, pty):
    session = open_ok(manager, pty)

    # The board talks. Nobody has called read() -- nobody has even asked.
    pty.send(b"U-Boot 2021.01\r\nHit any key to stop autoboot:\r\n")

    # The proof that capture is already running: head advances with no read.
    until(lambda: session.buffer.head > 0, what="the reader to capture with no read() call")
    until(lambda: b"autoboot" in session.buffer.read(0)[0], what="the whole line to arrive")

    data, next_cursor, dropped, remaining = session.buffer.read(0)
    assert b"U-Boot 2021.01" in data
    assert dropped == 0
    assert next_cursor == session.buffer.head
    assert remaining == 0


def test_open_does_not_return_before_the_reader_is_running(manager, pty):
    session = open_ok(manager, pty)
    # Not "a thread was created" -- a thread that is alive the instant open returned.
    assert session._reader.is_alive()


# --------------------------------------------------------------------------
# Invariant 1: one session per worker. No queue, no steal.
# --------------------------------------------------------------------------

def test_a_second_open_is_refused_and_names_the_live_session(manager, pty, second_pty):
    first = open_ok(manager, pty)
    time.sleep(0.05)  # so the reported age is visibly non-zero

    with pytest.raises(SessionError) as e:
        manager.open(device=second_pty.by_id, expect_serial=None,
                     baud=9600, flow="none", dtr=False, rts=False)

    message = str(e.value)
    assert first.id in message, "the refusal must name the session holding the port"
    assert pty.by_id in message, "...and the device it holds"
    assert "age" in message.lower(), "...and how long it has held it"

    # It did not steal: the first session is untouched and still capturing.
    assert manager.current is first
    assert first.alive
    pty.send(b"still mine\r\n")
    until(lambda: b"still mine" in first.buffer.read(0)[0], what="the first session to keep capturing")

    # It did not queue, and it did not half-open the second device either.
    assert can_open_exclusively(os.path.realpath(second_pty.by_id)), \
        "the refused open must not have taken the second port"


def test_a_second_open_is_refused_even_when_the_first_session_is_dead(manager, pty, second_pty):
    """Invariant 7 meets invariant 1: a dead session still holds the slot."""
    first = open_ok(manager, pty)
    pty.unplug_far_end()
    until(lambda: not first.alive, what="the session to notice the device died")

    with pytest.raises(SessionError) as e:
        manager.open(device=second_pty.by_id, expect_serial=None,
                     baud=115200, flow="none", dtr=False, rts=False)
    assert first.id in str(e.value)
    assert manager.current is first


# --------------------------------------------------------------------------
# Invariants 5 and 6: a vanished device is named, and the buffer outlives it.
# --------------------------------------------------------------------------

def test_closing_the_far_end_marks_the_session_dead_and_keeps_the_capture(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"panic: kernel oops at 0xdeadbeef\r\n")
    until(lambda: b"0xdeadbeef" in session.buffer.read(0)[0], what="the pre-death capture")

    pty.unplug_far_end()
    until(lambda: not session.alive, what="the reader to notice the far end closed")

    # Named, not a bare False.
    assert session.death_reason is not None
    assert pty.by_id in session.death_reason, "the failure must name the by-id path"

    # Invariant 6: what was captured before the unplug is still evidence.
    data, _, dropped, _ = session.buffer.read(0)
    assert b"0xdeadbeef" in data
    assert dropped == 0

    # Invariant 7: the session is still there. It was not reaped.
    assert manager.current is session
    assert manager.get(session.id) is session


def test_an_unplugged_by_id_path_is_detected_and_named(manager, pty):
    """The quiet unplug: the node stops resolving but no read has errored yet."""
    session = open_ok(manager, pty)
    pty.send(b"pre-unplug\r\n")
    until(lambda: b"pre-unplug" in session.buffer.read(0)[0], what="the pre-unplug capture")

    pty.unlink_by_id()
    until(lambda: not session.alive, what="the reader to notice the by-id path vanished")

    assert session.death_reason is not None
    assert pty.by_id in session.death_reason
    assert b"pre-unplug" in session.buffer.read(0)[0]


def test_a_dead_device_reads_as_evidence_not_as_silence(manager, pty):
    """Invariant 5: distinguishable from a quiet target, and it does not hang."""
    session = open_ok(manager, pty)
    pty.send(b"last words\r\n")
    until(lambda: b"last words" in session.buffer.read(0)[0], what="the capture")
    pty.unplug_far_end()
    until(lambda: not session.alive, what="death")

    data, _, dropped, _ = session.buffer.read(0)
    assert data != b"", "a dead device must not read back as an empty, quiet line"
    assert dropped == 0

    status = manager.status()
    assert status["alive"] is False
    assert status["death_reason"] is not None
    # A quiet-but-healthy target is the case this must NOT look like.
    assert status["open"] is True


def test_writing_to_a_dead_session_is_refused_by_name(manager, pty):
    session = open_ok(manager, pty)
    pty.unplug_far_end()
    until(lambda: not session.alive, what="death")

    with pytest.raises(SessionError) as e:
        session.write(b"reboot\r")
    assert session.id in str(e.value)
    assert session.death_reason is not None and session.death_reason in str(e.value)


# --------------------------------------------------------------------------
# Invariant 3: DTR and RTS are set explicitly and reported.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dtr,rts", [(False, False), (True, True), (True, False), (False, True)])
def test_open_reports_the_dtr_and_rts_it_set(manager, pty, dtr, rts):
    session = open_ok(manager, pty, dtr=dtr, rts=rts)

    assert session.dtr is dtr
    assert session.rts is rts
    status = manager.status()
    assert status["dtr"] is dtr and status["rts"] is rts

    # Not merely recorded on the session object: pushed into the port config,
    # which is what pyserial applies with TIOCMBIS/TIOCMBIC at open().
    assert session._serial.dtr is dtr
    assert session._serial.rts is rts


def test_dtr_and_rts_are_not_inherited_from_the_driver_default(manager, pty):
    """pyserial's own default asserts both lines. Many boards reset on DTR."""
    unconfigured = serial.Serial()
    assert unconfigured.dtr is True and unconfigured.rts is True, \
        "baseline: the driver default this invariant exists to override"

    session = open_ok(manager, pty, dtr=False, rts=False)
    assert session._serial.dtr is False
    assert session._serial.rts is False


# --------------------------------------------------------------------------
# Invariant 4: flow control is explicit, defaulting to none.
# --------------------------------------------------------------------------

def test_flow_defaults_to_none(manager, pty):
    session = manager.open(device=pty.by_id, expect_serial=SERIAL,
                           baud=115200, dtr=False, rts=False)
    assert session.flow == "none"
    assert session._serial.rtscts is False
    assert session._serial.xonxoff is False
    assert session._serial.dsrdtr is False


@pytest.mark.parametrize("flow,attr", [("rtscts", "rtscts"), ("xonxoff", "xonxoff")])
def test_a_named_flow_mode_reaches_the_port(manager, pty, flow, attr):
    session = open_ok(manager, pty, flow=flow)
    assert session.flow == flow
    assert getattr(session._serial, attr) is True
    assert manager.status()["flow"] == flow


def test_rtscts_reports_rts_as_driver_controlled_rather_than_as_requested(manager, pty):
    """pyserial skips `_update_rts_state()` when rtscts is on.

    So the requested RTS is never applied, and echoing it back would be a
    false report about a line that resets boards. `None` is the true answer.
    """
    session = open_ok(manager, pty, flow="rtscts", rts=True)
    assert session.rts is None
    assert manager.status()["rts"] is None
    # DTR is still ours: dsrdtr stays off, which is the condition pyserial
    # applies `_update_dtr_state()` under.
    assert session.dtr is False
    assert session._serial.dtr is False


def test_an_unknown_flow_mode_is_refused_before_the_port_is_opened(manager, pty):
    with pytest.raises(SessionError) as e:
        open_ok(manager, pty, flow="hardware")
    message = str(e.value)
    assert "hardware" in message
    assert "none" in message and "rtscts" in message and "xonxoff" in message

    assert manager.current is None, "a rejected open must not occupy the session slot"
    assert can_open_exclusively(os.path.realpath(pty.by_id)), \
        "a rejected open must not leave the port held"


# --------------------------------------------------------------------------
# Resolution failures happen before the port is touched.
# --------------------------------------------------------------------------

def test_a_serial_mismatch_is_refused_and_leaves_the_slot_free(manager, pty):
    with pytest.raises(DeviceError) as e:
        manager.open(device=pty.by_id, expect_serial="TG0000aa",
                     baud=115200, flow="none", dtr=False, rts=False)
    assert "TG0000aa" in str(e.value)

    assert manager.current is None
    assert can_open_exclusively(os.path.realpath(pty.by_id))

    # And the slot really is free: the correct open still works.
    session = open_ok(manager, pty)
    assert manager.current is session


def test_a_port_already_held_by_someone_else_is_a_named_refusal(manager, pty):
    holder = serial.Serial()
    holder.port = os.path.realpath(pty.by_id)
    holder.exclusive = True
    holder.timeout = 0.05
    holder.open()
    try:
        with pytest.raises(SessionError) as e:
            open_ok(manager, pty)
        assert pty.by_id in str(e.value)
        assert manager.current is None
    finally:
        holder.close()


# --------------------------------------------------------------------------
# status() and the unknown-id errors.
# --------------------------------------------------------------------------

def test_status_with_no_session_open_is_a_valid_answer(manager):
    status = manager.status()
    assert isinstance(status, dict)
    assert status["open"] is False
    assert status["session"] is None
    # Every key a caller reads is present whether or not a session exists, so
    # "no session" is answered, not raised and not KeyError'd downstream.
    assert set(manager.status()) == set(SessionManager.STATUS_KEYS)


def test_status_reports_the_open_session(manager, pty):
    session = open_ok(manager, pty, baud=9600)
    pty.send(b"x" * 10)
    until(lambda: session.buffer.head == 10, what="capture")

    status = manager.status()
    assert set(status) == set(SessionManager.STATUS_KEYS)
    assert status["open"] is True
    assert status["session"] == session.id
    assert status["device"] == pty.by_id
    assert status["serial"] == SERIAL
    assert status["baud"] == 9600
    assert status["alive"] is True
    assert status["death_reason"] is None
    assert status["buffer_head"] == 10
    assert status["dropped"] == 0
    assert status["age_s"] >= 0.0


def test_close_on_an_unknown_id_names_the_id(manager):
    with pytest.raises(KeyError) as e:
        manager.close("sess-nosuchthing")
    assert "sess-nosuchthing" in str(e.value)


def test_close_on_the_wrong_id_names_it_and_spares_the_live_session(manager, pty):
    session = open_ok(manager, pty)
    with pytest.raises(KeyError) as e:
        manager.close("sess-notmine")
    assert "sess-notmine" in str(e.value)
    assert manager.current is session
    assert session.alive


def test_get_on_an_unknown_id_raises_keyerror(manager, pty):
    open_ok(manager, pty)
    with pytest.raises(KeyError):
        manager.get("sess-notmine")


# --------------------------------------------------------------------------
# Invariant 7: close is the only thing that ends a session.
# --------------------------------------------------------------------------

def test_no_public_operation_but_close_ends_a_session(manager, pty):
    """Runs every callable surface that can be called without an id-guess.

    Written as a sweep rather than a fixed list so that a later task adding a
    method to SessionManager is covered by this test the day it lands.
    """
    session = open_ok(manager, pty)
    pty.send(b"evidence\r\n")
    until(lambda: b"evidence" in session.buffer.read(0)[0], what="capture")

    def exercise_everything():
        for name in dir(manager):
            if name.startswith("_") or name in ("open", "close"):
                continue
            attribute = getattr(manager, name)
            if not callable(attribute):
                continue
            signature = inspect.signature(attribute)
            required = [p for p in signature.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
            if len(required) == 1 and "session" in required[0].name:
                attribute(session.id)
            elif not required:
                attribute()
        session.buffer.read(0)
        manager.current  # noqa: B018 -- the property is part of the surface

    exercise_everything()
    assert manager.current is session and session.alive

    # And again once the device is gone: a dead session is still a session.
    pty.unplug_far_end()
    until(lambda: not session.alive, what="death")
    exercise_everything()
    assert manager.current is session
    assert b"evidence" in session.buffer.read(0)[0]

    manager.close(session.id)
    assert manager.current is None


def test_close_releases_the_port_and_stops_the_reader(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"bye\r\n")
    until(lambda: session.buffer.head > 0, what="capture")

    manager.close(session.id)

    assert manager.current is None
    with pytest.raises(KeyError):
        manager.get(session.id)
    assert not session._reader.is_alive(), "close must join the reader, not orphan it"
    assert can_open_exclusively(os.path.realpath(pty.by_id)), \
        "close must release the exclusive lock on the port"


def test_close_discards_the_capture(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"gone after close\r\n")
    until(lambda: session.buffer.head > 0, what="capture")
    manager.close(session.id)

    # The contract says the buffer is discarded on close. Reading it must say
    # so rather than hand back a stale capture from a session that is over.
    with pytest.raises(SessionError):
        session.buffer.read(0)


def test_close_is_reported_as_unknown_the_second_time(manager, pty):
    session = open_ok(manager, pty)
    manager.close(session.id)
    with pytest.raises(KeyError):
        manager.close(session.id)


# --------------------------------------------------------------------------
# Invariant 9: a daemon disconnect does not end the session.
# --------------------------------------------------------------------------

def test_a_session_survives_a_client_disconnect_and_is_adopted(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"boot log worth keeping\r\n")
    until(lambda: b"worth keeping" in session.buffer.read(0)[0], what="capture")
    session_id = session.id
    head_before = session.buffer.head

    # The daemon goes away: every reference the client side held is dropped.
    # If the session's life were tied to a client handle -- a weakref, a
    # __del__, a context manager -- this is where it would end.
    del session
    gc.collect()

    pty.send(b"and more after the blip\r\n")
    time.sleep(0.05)

    # The daemon reconnects and adopts what is still running.
    adopted = manager.get(session_id)
    assert adopted.alive
    assert manager.status()["session"] == session_id
    until(lambda: b"after the blip" in adopted.buffer.read(0)[0],
          what="capture to have continued across the disconnect")
    assert adopted.buffer.head > head_before

    data, _, dropped, _ = adopted.buffer.read(0)
    assert b"worth keeping" in data, "the pre-disconnect capture is the evidence"
    assert dropped == 0


# --------------------------------------------------------------------------
# Invariant 8: reads are served from the buffer, and the buffer is synchronised.
# --------------------------------------------------------------------------

def test_reads_are_served_from_the_buffer_not_the_device(manager, pty):
    """The sharpest available proof: the device is unusable and reads work."""
    session = open_ok(manager, pty)
    pty.send(b"captured while alive\r\n")
    until(lambda: b"while alive" in session.buffer.read(0)[0], what="capture")
    pty.unplug_far_end()
    until(lambda: not session.alive, what="death")

    # If read() touched the port it would raise here; the port is gone.
    for _ in range(5):
        data, cursor, dropped, remaining = session.buffer.read(0)
        assert b"captured while alive" in data
        assert dropped == 0 and remaining == 0


def test_the_capture_buffer_serialises_readers_against_the_writer(manager, pty):
    """White-box: `read` and `append` must contend on one lock.

    `CaptureBuffer` ships with no locking -- deliberately, see its module
    docstring -- so the session layer owns it. This asserts the mechanism
    rather than hoping a race shows up: hold the buffer's lock and both
    operations must wait.
    """
    session = open_ok(manager, pty)
    buffer = session.buffer
    assert isinstance(buffer, CaptureBuffer)
    lock = buffer._lock

    for operation in (lambda: buffer.read(0), lambda: buffer.append(b"z")):
        started = threading.Event()
        finished = threading.Event()

        def run():
            started.set()
            operation()
            finished.set()

        with lock:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            assert started.wait(DEADLINE)
            assert not finished.wait(0.2), "the operation did not take the buffer lock"
        assert finished.wait(DEADLINE), "the operation did not proceed once the lock was free"
        thread.join(DEADLINE)


def test_concurrent_reads_during_a_flood_stay_byte_exact(manager, pty):
    """Smoke test, not a proof: a race is not deterministic.

    It does pin the one thing that must hold -- a cursor-following consumer
    sees every byte, in order, exactly once -- while other threads hammer the
    same buffer and the reader thread appends into it.
    """
    session = open_ok(manager, pty)
    chunks = [f"line {i:04d} ".encode() + bytes([65 + (i % 26)]) * 200 + b"\r\n"
              for i in range(200)]
    expected = b"".join(chunks)

    collected = bytearray()
    stop = threading.Event()
    errors: list[BaseException] = []

    def consumer():
        cursor = 0
        try:
            while not stop.is_set() or cursor < session.buffer.head:
                data, cursor, dropped, _ = session.buffer.read(cursor, 4096)
                assert dropped == 0
                collected.extend(data)
                if not data:
                    time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001 -- reported, not swallowed
            errors.append(exc)

    def hammer():
        try:
            while not stop.is_set():
                session.buffer.read(0, 512)
                manager.status()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=consumer, daemon=True)]
    threads += [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()

    for chunk in chunks:
        pty.send(chunk)

    until(lambda: session.buffer.head == len(expected), what="every byte to be captured")
    stop.set()
    for t in threads:
        t.join(DEADLINE)

    assert not errors, errors
    assert bytes(collected) == expected


# --------------------------------------------------------------------------
# write(), which tasks 5 and 6 depend on.
# --------------------------------------------------------------------------

# A pty's output buffer is a few KiB. Nobody drains the master in these tests,
# so a blob this size is guaranteed to leave `write` blocked in the port --
# which is the only way to get a write in flight deterministically.
WEDGE = b"S" * (1 << 20)


def drain(pty, rounds: int = 50) -> None:
    while rounds and pty.recv(0.02):
        rounds -= 1


def _instrument(session):
    """Count writers inside `serial.write()`, and snapshot that at `close()`.

    The contract is not "close waits a while", it is "the fd is not released
    while a writer is inside the port". This measures exactly that.
    """
    state = {"inflight": 0, "peak": 0, "inflight_at_close": None, "closed": False}
    guard = threading.Lock()
    real_write, real_close = session._serial.write, session._serial.close

    def counting_write(data):
        with guard:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            return real_write(data)
        finally:
            with guard:
                state["inflight"] -= 1

    def counting_close():
        with guard:
            state["inflight_at_close"] = state["inflight"]
            state["closed"] = True
        return real_close()

    session._serial.write = counting_write
    session._serial.close = counting_close
    return state


def test_concurrent_writes_and_a_close_never_release_the_fd_under_a_writer(
        manager, pty, second_pty):
    """Three writers, not one -- one writer cannot show the real failure.

    `write` consults the session's closing state only *after* it holds
    `_write_lock`, so without a flag set before `close` queues for that lock,
    every writer waiting on it starts a fresh `write_timeout` behind the
    closer. Three of them outlast any bound `close` could reasonably wait, and
    the fd is then released with a writer still inside `serial.write()` -- at
    which point the next `open` can be handed the same fd number and this
    session's bytes go to the next session's board.
    """
    session = open_ok(manager, pty)
    state = _instrument(session)

    raised: list[tuple[str, str]] = []
    finished = threading.Event()
    done = 0
    tally = threading.Lock()

    def writer():
        nonlocal done
        try:
            session.write(WEDGE)
        except BaseException as exc:  # noqa: BLE001 -- the type is the assertion
            raised.append((type(exc).__name__, str(exc)))
        finally:
            with tally:
                done += 1
                if done == 3:
                    finished.set()

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    assert not finished.is_set(), "the writes were not wedged; the test proves nothing"

    started = time.monotonic()
    manager.close(session.id)
    elapsed = time.monotonic() - started

    # The assertion the whole fix exists for.
    assert state["closed"], "the port was never closed"
    assert state["inflight_at_close"] == 0, (
        f"the fd was released with {state['inflight_at_close']} writer(s) "
        "inside serial.write()"
    )
    # The drain bound must not have been reached: queued writers refuse
    # instantly once the session is closing, so only one write is outstanding.
    assert manager.last_close_warning is None, manager.last_close_warning
    assert elapsed < WRITE_DRAIN_TIMEOUT, f"close took {elapsed:.2f}s"
    # The serialisation `_write_lock` exists for, asserted directly rather
    # than inferred from how long the close took.
    assert state["peak"] == 1, \
        f"{state['peak']} writers were inside serial.write() at once"

    assert finished.wait(DEADLINE)
    for thread in threads:
        thread.join(DEADLINE)

    # Non-vacuous by construction: assert the list is populated before
    # asserting anything about its contents. Every writer must have been told
    # it did not write, one way or another.
    assert len(raised) == 3, raised
    assert all(kind == "SessionError" for kind, _ in raised), raised

    # And the harm: the closed session's bytes must never reach the next board.
    later = open_ok(manager, second_pty)
    assert b"S" not in second_pty.recv(0.2), \
        "bytes from the closed session reached the next session's target"
    assert later.alive


def test_a_write_outlasting_the_drain_bound_leaks_the_port_rather_than_stealing_it(
        manager, pty, monkeypatch):
    """The branch fix (a) makes unreachable, tested directly anyway.

    If a write somehow outlasts the drain bound, `close` must NOT release the
    fd: the next `open` could be handed the same fd number and this session's
    bytes would go to the next board. Leaking the port until the write really
    finishes is strictly safer -- a held port is visible and recoverable by
    restarting the worker; writing to the wrong physical target is neither.
    """
    import pare_hardware_mcp.session as session_module
    monkeypatch.setattr(session_module, "WRITE_DRAIN_TIMEOUT", 0.2)

    session = open_ok(manager, pty)
    state = _instrument(session)

    with session._write_lock:  # stands in for a write that will not finish
        manager.close(session.id)

        assert not state["closed"], \
            "close released the fd with the write lock held by someone else"
        assert manager.last_close_warning is not None
        assert pty.by_id in manager.last_close_warning, \
            "the warning must name the port that is still held"
        assert manager.current is None, "the session is over regardless"

    # ...and once the write finishes, the port is released without being asked.
    until(lambda: state["closed"], what="the drain thread to release the port")
    assert can_open_exclusively(os.path.realpath(pty.by_id))


def test_a_cancelled_write_is_not_reported_as_a_successful_send(manager, pty):
    """`cancel_write()` aborts pyserial's write loop and raises NOTHING.

    serialposix.py:632-634 is `os.read(pipe_abort_write_r, 1000); break`, and
    :662 is `return length - len(d)` -- the short count comes back in the
    return value. Discard it and a send that put zero bytes on the wire
    reports success to a language model, which is a false claim about the
    physical state of a board.
    """
    session = open_ok(manager, pty)
    outcome: list[tuple[str, str]] = []
    returned = threading.Event()

    def writer():
        try:
            session.write(WEDGE)
            outcome.append(("returned", "no exception"))
        except BaseException as exc:  # noqa: BLE001
            outcome.append((type(exc).__name__, str(exc)))
        finally:
            returned.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert not returned.is_set(), "the write was not wedged; the test proves nothing"

    session._serial.cancel_write()
    assert returned.wait(DEADLINE)
    thread.join(DEADLINE)

    assert len(outcome) == 1
    kind, message = outcome[0]
    assert kind == "SessionError", f"an aborted write reported success: {outcome}"
    # Say how much actually went out, not just that something went wrong.
    assert str(len(WEDGE)) in message, message
    # An abort is not a device failure.
    assert session.alive is True
    assert session.death_reason is None


def test_an_aborted_write_reports_a_lower_bound_not_a_prefix_boundary(manager, pty):
    """pyserial's abort count is short by one chunk, always.

    `break` on abort (serialposix.py:633) happens before `d = d[n:]` (:658),
    and every iteration of the loop writes before it selects -- so the final
    `os.write` is never deducted from `length - len(d)`. Reporting that count
    as the exact prefix the target received would invite a caller to resume
    from the offset and resend bytes that already reached the board.
    """
    session = open_ok(manager, pty)
    stop = threading.Event()
    pump = pty.drain_slowly(stop)
    blob = b"S" * (4 << 20)  # far more than the slow drain can take in time

    outcome: list[BaseException] = []
    returned = threading.Event()

    def writer():
        try:
            session.write(blob)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)
        finally:
            returned.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert not returned.is_set(), "the write finished; the test proves nothing"

    session._serial.cancel_write()
    assert returned.wait(DEADLINE)
    thread.join(DEADLINE)
    time.sleep(0.2)
    stop.set()
    pump.join(DEADLINE)
    pty.drain_rest()

    assert len(outcome) == 1 and isinstance(outcome[0], SessionError), outcome
    message = str(outcome[0])
    assert "at least" in message, \
        f"an aborted write must not claim an exact boundary: {message}"

    reported = int(re.search(r"at least (\d+) of", message).group(1))
    # The measurement that makes the wording necessary rather than cautious:
    # more bytes physically reached the target than the count names.
    assert len(pty.received) > reported, (
        f"reported at least {reported}, target received {len(pty.received)} "
        "-- expected the count to under-report"
    )
    assert len(pty.received) < len(blob)


def test_a_concurrent_open_during_a_close_is_told_why_it_failed(manager, pty):
    """The window `last_close_warning` exists for is the one it must cover.

    `close` frees the slot under the manager lock and shuts down outside it.
    An `open` arriving in between gets past the one-session check and then
    fails on the port's exclusive lock. Clearing the warning to None at the
    top of `close` left that open with a bare EAGAIN -- exactly what the hint
    was added to prevent.
    """
    session = open_ok(manager, pty)
    closed = threading.Event()

    def closer():
        try:
            manager.close(session.id)
        finally:
            closed.set()

    with session._write_lock:  # stalls _shutdown where an in-flight write would
        thread = threading.Thread(target=closer, daemon=True)
        thread.start()
        time.sleep(0.2)
        assert not closed.is_set(), "close did not stall; the test proves nothing"
        assert manager.current is None, "the slot must already be free"

        assert manager.last_close_warning is not None, \
            "nothing published to explain a failure inside this window"
        with pytest.raises(SessionError) as e:
            open_ok(manager, pty)
        message = str(e.value)
        assert pty.by_id in message
        assert session.id in message, \
            "the failure must name the session still holding the port"
        assert "clos" in message, f"no mention of the close in flight: {message}"

    assert closed.wait(DEADLINE)
    thread.join(DEADLINE)
    # Once the shutdown really finishes, a clean close leaves no warning behind.
    assert manager.last_close_warning is None
    assert can_open_exclusively(os.path.realpath(pty.by_id))


def test_close_does_not_hold_the_manager_lock_across_the_shutdown(manager, pty):
    """`status`, `get` and `open` are tool calls; `close` can wait seconds.

    Ordering, not duration: `status()` must return while `close` is still
    inside `_shutdown`. Holding the manager lock across the shutdown makes
    that impossible.
    """
    session = open_ok(manager, pty)
    closed = threading.Event()
    failures: list[BaseException] = []

    def closer():
        try:
            manager.close(session.id)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            closed.set()

    with session._write_lock:  # stalls _shutdown exactly where a write would
        thread = threading.Thread(target=closer, daemon=True)
        thread.start()
        time.sleep(0.2)
        assert not closed.is_set(), "close did not stall; the test proves nothing"

        status = manager.status()  # must not queue behind the stalled close
        assert not closed.is_set(), \
            "status() only returned after close did -- the manager lock was " \
            "held across the shutdown"
        assert set(status) == set(SessionManager.STATUS_KEYS)
        with pytest.raises(KeyError):
            manager.get(session.id)

    assert closed.wait(DEADLINE)
    thread.join(DEADLINE)
    assert not failures, failures
    assert manager.current is None


def test_write_after_close_is_refused(manager, pty):
    session = open_ok(manager, pty)
    manager.close(session.id)
    with pytest.raises(SessionError):
        session.write(b"too late\r\n")


def test_a_write_timeout_does_not_mark_a_healthy_session_dead(manager, pty):
    """`SerialTimeoutException` subclasses `SerialException` (serialutil.py:96).

    Catching it with the rest and calling `_die` would brick the console for
    the rest of the session -- and claim the device is gone while the reader
    is still capturing from it. This is the live failure mode under
    flow="rtscts" against a target that never asserts CTS, which is the exact
    case invariant 4 exists for.
    """
    session = open_ok(manager, pty)
    pty.send(b"boot log\r\n")
    until(lambda: b"boot log" in session.buffer.read(0)[0], what="capture")

    session._serial.write_timeout = 0.2  # 5s is the shipped value; too slow here
    with pytest.raises(SessionError) as e:
        session.write(WEDGE)
    # Assert the relationship, not the wording: the refusal must be the port's
    # own timeout surfaced as a SessionError, not a death.
    assert isinstance(e.value.__cause__, serial.SerialTimeoutException)
    assert session.id in str(e.value)

    # A bounded, recoverable failure -- not a death.
    assert session.alive is True
    assert session.death_reason is None
    assert manager.status()["alive"] is True
    assert manager.status()["death_reason"] is None

    # The reader never stopped, and the console is not write-bricked.
    pty.send(b"target is still talking fine\r\n")
    until(lambda: b"still talking fine" in session.buffer.read(0)[0],
          what="the reader to carry on across the write timeout")
    drain(pty)
    session._serial.write_timeout = 5.0
    session.write(b"and still writable\r\n")


def test_a_real_write_failure_still_marks_the_session_dead(manager, pty):
    """The other half: a timeout is recoverable, a vanished device is not.

    The reader is silenced first so that the *write* path is the only thing
    that can notice -- otherwise the reader wins the race every time and this
    would pass with `_die` deleted from `write`.
    """
    session = open_ok(manager, pty)
    session._stop.set()
    session._serial.cancel_read()
    session._reader.join(DEADLINE)
    assert not session._reader.is_alive()
    assert session.alive, "the reader, not the unplug, must be what stopped"

    pty.unplug_far_end()
    with pytest.raises(SessionError) as e:
        session.write(b"anyone there?\r\n")

    # EIO, not a timeout -- the two branches must stay distinguishable.
    assert not isinstance(e.value.__cause__, serial.SerialTimeoutException)
    assert session.alive is False
    assert session.death_reason is not None
    assert pty.by_id in session.death_reason


def test_close_blocks_on_the_write_lock(manager, pty):
    """White-box, deterministic: `close` must contend with `write`.

    The behavioural test above can be satisfied by `cancel_write()` alone,
    which only *requests* an abort and gives no ordering guarantee. This
    asserts the lock itself, the way the buffer-lock test does.
    """
    session = open_ok(manager, pty)
    closed = threading.Event()
    failures: list[BaseException] = []

    def closer():
        try:
            manager.close(session.id)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            closed.set()

    with session._write_lock:
        thread = threading.Thread(target=closer, daemon=True)
        thread.start()
        assert not closed.wait(0.3), \
            "close released the port without waiting for the write lock"
    assert closed.wait(DEADLINE), "close did not proceed once the lock was free"
    thread.join(DEADLINE)
    assert not failures, failures
    assert manager.current is None


def test_write_takes_the_lock_before_deciding_anything(manager, pty):
    """White-box, deterministic: the liveness checks are inside `_write_lock`.

    Checked outside, they are check-then-act: `_shutdown` can close the fd in
    the gap between the check passing and the write starting. A closed session
    is used because it makes the two placements behave differently -- with the
    checks outside the lock, `write` refuses instantly without ever touching
    it.
    """
    session = open_ok(manager, pty)
    lock = session._write_lock
    manager.close(session.id)

    returned = threading.Event()
    outcome: list[str] = []

    def writer():
        try:
            session.write(b"x")
            outcome.append("returned")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(type(exc).__name__)
        finally:
            returned.set()

    with lock:
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        assert not returned.wait(0.3), \
            "write decided without taking the write lock first"
    assert returned.wait(DEADLINE)
    thread.join(DEADLINE)
    assert outcome == ["SessionError"], outcome


def test_a_failure_building_the_session_still_releases_the_port(manager, pty, monkeypatch):
    """The 64 MiB capture buffer is allocated while the port is already open.

    `serial.Serial` inherits `io.RawIOBase.__del__`, so a stranded port is
    eventually closed by refcounting -- which is why this asserts on the port
    object itself rather than on whether the tty can be reopened. Holding a
    reference to it here is exactly the situation a live traceback or a
    reference cycle creates in the worker: until the last reference goes, the
    fd holds flock(LOCK_EX) on the tty while `_current` is still None, so the
    manager reports an idle bench whose port nothing can open. `open` must
    close it, not leave the one exclusive resource on this worker to the
    garbage collector.
    """
    import pare_hardware_mcp.session as session_module

    opened: list[serial.Serial] = []
    real_open_port = SessionManager._open_port

    def spy(self, resolved, **kwargs):
        port = real_open_port(self, resolved, **kwargs)
        opened.append(port)
        return port

    def explode(*args, **kwargs):
        raise MemoryError("no room for the capture buffer")

    monkeypatch.setattr(SessionManager, "_open_port", spy)
    monkeypatch.setattr(session_module.ConsoleSession, "__init__", explode)

    with pytest.raises(MemoryError):
        open_ok(manager, pty)

    assert opened, "the port was never opened; this test would prove nothing"
    assert opened[0].is_open is False, \
        "the port was left open by a failed open() -- it holds flock(LOCK_EX) " \
        "on the tty for as long as anything references it"
    assert manager.current is None
    assert can_open_exclusively(os.path.realpath(pty.by_id))



def test_write_puts_bytes_on_the_line(manager, pty):
    session = open_ok(manager, pty)
    session.write(b"help\r")
    received = bytearray()

    def arrived():
        received.extend(pty.recv(0.1))
        return b"help\r" in bytes(received)

    until(arrived, what="the target to receive the write")


# --------------------------------------------------------------------------
# scan_baud -- Task 7. A pty has no real UART framing, so these tests cannot
# prove a candidate rate is physically wrong; they prove the COORDINATION
# invariants: the reader is never racing the scan for bytes, a scan sample
# never reaches the capture buffer, the port is reconfigured rather than
# reopened, nothing is ever transmitted, and close()/write() still behave
# correctly around a scan in flight.
# --------------------------------------------------------------------------

def no_op_decide(samples):
    return None


def test_scan_baud_rejects_a_zero_rate_before_touching_the_port(manager, pty):
    session = open_ok(manager, pty, baud=9600)
    with pytest.raises(SessionError) as e:
        session.scan_baud((9600, 0, 115200), 0.1, no_op_decide)
    assert "0" in str(e.value)

    # Nothing was touched: still capturing at the original rate.
    assert session.baud == 9600
    assert session._serial.baudrate == 9600
    pty.send(b"still alive\r\n")
    until(lambda: b"still alive" in session.buffer.read(0)[0],
          what="capture unaffected by the refused scan")


def test_scan_baud_rejects_a_negative_or_non_integer_rate(manager, pty):
    session = open_ok(manager, pty)
    for bad_rates in ((9600, -1), (9600, True), (9600, 3.5), (9600, MAX_BAUD_RATE + 1)):
        with pytest.raises(SessionError):
            session.scan_baud(bad_rates, 0.1, no_op_decide)


def test_scan_baud_rejects_an_empty_rate_list(manager, pty):
    session = open_ok(manager, pty)
    with pytest.raises(SessionError):
        session.scan_baud((), 0.1, no_op_decide)


def test_scan_baud_refuses_on_a_dead_session(manager, pty):
    session = open_ok(manager, pty)
    pty.unplug_far_end()
    until(lambda: not session.alive, what="death")
    with pytest.raises(SessionError):
        session.scan_baud((9600, 115200), 0.1, no_op_decide)


def test_scan_baud_never_writes_to_the_target(manager, pty):
    """Invariant 3: detection never transmits."""
    session = open_ok(manager, pty)
    real_write = session._serial.write
    calls = []

    def spy(data):
        calls.append(bytes(data))
        return real_write(data)

    session._serial.write = spy
    session.scan_baud((9600, 115200, 230400), 0.15, no_op_decide)
    assert calls == [], f"scan_baud transmitted: {calls!r}"


def test_scan_baud_never_closes_and_reopens_the_port(manager, pty):
    """Invariant 1: change speed via termios on the held fd, never reopen.

    NOT testable by comparing fd numbers before and after: reopening the
    SAME Serial object preserves object identity, and `os.open` hands back
    the lowest free fd -- which, immediately after `close()`, is the one
    just released. A close-then-reopen-per-candidate mutant passes both
    `session._serial is serial_obj` and `fileno() == fd_before` -- confirmed
    by mutating scan_baud to do exactly that; the old version of this test
    passed against it. Spying on `open`/`close` observes the effect that
    actually matters instead.
    """
    session = open_ok(manager, pty)
    serial_obj = session._serial
    open_calls = []
    close_calls = []
    real_open, real_close = serial_obj.open, serial_obj.close
    serial_obj.open = lambda *a, **k: (open_calls.append(1), real_open(*a, **k))[1]
    serial_obj.close = lambda *a, **k: (close_calls.append(1), real_close(*a, **k))[1]

    session.scan_baud((9600, 115200, 230400), 0.1, lambda s: 115200)

    assert open_calls == [], f"scan_baud opened the port {len(open_calls)} time(s)"
    assert close_calls == [], f"scan_baud closed the port {len(close_calls)} time(s)"
    assert session._serial is serial_obj
    assert session._serial.is_open


def test_scan_baud_leaves_the_port_at_the_decided_winner(manager, pty):
    session = open_ok(manager, pty, baud=9600)
    result = session.scan_baud((9600, 115200, 230400), 0.1, lambda s: 115200)

    assert result["final_baud"] == 115200
    assert result["original_baud"] == 9600
    assert result["restored"] is False
    assert session.baud == 115200
    assert session._serial.baudrate == 115200


def test_scan_baud_reports_a_winner_that_matches_the_original_rate_as_a_win(manager, pty):
    """`restored` must mean 'no winner was found', not 'the rate is unchanged'.

    A `decide` that correctly confirms the session's current rate is a real
    winner -- conflating it with the fallback-restore case would tell a
    caller the scan found nothing when it actually confirmed the rate.
    """
    session = open_ok(manager, pty, baud=9600)
    result = session.scan_baud((9600, 115200), 0.1, lambda s: 9600)

    assert result["winner"] == 9600
    assert result["final_baud"] == 9600
    assert result["restored"] is False


# --------------------------------------------------------------------------
# Critical 1: the pause is a real capture gap, and it must be visible --
# never as an in-band buffer marker (the capture must stay byte-exact
# evidence), always out-of-band and surfaced at the point of reading.
# --------------------------------------------------------------------------

def test_scan_baud_records_a_gap_spanning_the_whole_suspension(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"before the scan\r\n")
    until(lambda: session.buffer.head > 0, what="pre-scan capture")
    cursor_before = session.buffer.head

    result = session.scan_baud((9600, 115200), 0.2, no_op_decide)

    gap = result["gap"]
    assert gap is not None
    assert gap["at_cursor"] == cursor_before, \
        "the gap must start where capture actually stopped, not at head==0"
    assert gap["duration_s"] >= 0.2, \
        "two 0.2s candidates must show up as at least that much suspended time"
    assert gap["reason"] == "baud scan"


def test_a_gap_is_visible_in_status_as_a_running_count_and_total(manager, pty):
    session = open_ok(manager, pty)
    before = manager.status()
    assert before["capture_gap_count"] == 0
    assert before["capture_gap_seconds"] == 0
    assert before["capture_suspended"] is False

    session.scan_baud((9600, 115200), 0.1, no_op_decide)
    after_one = manager.status()
    assert after_one["capture_gap_count"] == 1
    assert after_one["capture_gap_seconds"] > 0
    assert after_one["capture_suspended"] is False, \
        "the scan already returned; nothing should still read as in progress"

    session.scan_baud((9600, 115200), 0.1, no_op_decide)
    after_two = manager.status()
    assert after_two["capture_gap_count"] == 2
    assert after_two["capture_gap_seconds"] > after_one["capture_gap_seconds"]
    assert after_two["capture_suspended"] is False


def test_a_gap_is_visible_while_the_scan_is_still_running(manager, pty):
    """N1: the entry must exist for the ~12s the scan is actually paused, not
    only after it returns -- a concurrent console_read/console_status during
    that window is exactly the reasoning hazard the ledger exists to prevent,
    just relocated to the live case instead of the retrospective one.
    """
    session = open_ok(manager, pty)
    pty.send(b"pre-scan\r\n")
    until(lambda: session.buffer.head > 0, what="pre-scan capture")
    cursor_before = session.buffer.head

    scan_done = threading.Event()

    def run_scan():
        session.scan_baud((9600, 115200, 230400), 0.3, no_op_decide)
        scan_done.set()

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    # NOT `until(lambda: session._scan_parked.is_set())`: that event is set
    # by the READER thread the instant it parks, but the gap entry is
    # appended by the SCAN thread separately, after IT wakes from
    # `_scan_parked.wait()` and reaches `_record_gap`. Measured: a real,
    # ~0.6ms window where `_scan_parked` already reads True but the ledger
    # is still empty (wider under cross-core contention) -- waiting on that
    # event was waiting for evidence only the OTHER thread produces. Waiting
    # on the ledger itself is the correct synchronisation.
    until(lambda: session._gaps, what="the scan to record its in-progress gap")

    # Checked only now that the ledger entry actually exists. Against a
    # pre-N1 implementation (gap recorded in the scan's `finally`, after the
    # whole call returns) this wait blocks until `scan_baud` has already
    # returned, so `scan_done` is already set and this still fails -- it
    # does not go vacuous.
    assert not scan_done.is_set(), "the gap only appeared after the scan returned"
    mid_scan_status = manager.status()
    assert mid_scan_status["capture_gap_count"] == 1, \
        "the gap must be counted the instant the reader parks, not only once the scan ends"
    # N2: `capture_gap_seconds` is 0.0 here (the in-progress entry's duration
    # is still its placeholder) -- pairing count:1 with seconds:0.0 reads as
    # "a gap of zero length", which is exactly as misleading as count:0. This
    # explicit flag is the signal a caller must use instead.
    assert mid_scan_status["capture_gap_seconds"] == 0.0
    assert mid_scan_status["capture_suspended"] is True
    mid_scan_gaps = session.gaps_overlapping(cursor_before, cursor_before + 1)
    assert len(mid_scan_gaps) == 1
    assert mid_scan_gaps[0]["in_progress"] is True
    assert mid_scan_gaps[0]["at_cursor"] == cursor_before

    thread.join(DEADLINE)
    assert scan_done.is_set()
    assert manager.status()["capture_suspended"] is False
    # And once it's over, the SAME entry is finalised, not duplicated.
    assert manager.status()["capture_gap_count"] == 1
    finished = session.gaps_overlapping(cursor_before, cursor_before + 1)
    assert len(finished) == 1
    assert finished[0]["in_progress"] is False
    assert finished[0]["duration_s"] >= 0.3


def test_a_failed_scan_still_records_its_gap(manager, pty):
    """The reader was still paused for the duration, even though the scan
    itself failed -- that suspended time must not disappear from the ledger
    just because nothing useful came of it."""
    session = open_ok(manager, pty)

    def exploding_decide(samples):
        raise RuntimeError("boom")

    with pytest.raises(SessionError):
        session.scan_baud((9600, 115200), 0.1, exploding_decide)

    assert manager.status()["capture_gap_count"] == 1


def test_finish_gap_guard_does_not_mask_an_exception_from_record_gap(manager, pty):
    """MINOR N4: `gap_start_time` is set one line before `gap_entry` in
    `scan_baud`. Guarding the `finally` on `gap_start_time is not None`
    (round 1/2's shape) means anything raising in that narrow window calls
    `_finish_gap(None, ...)`, a bare TypeError masking whatever actually
    went wrong. Guarding on `gap_entry is not None` instead means the real
    exception -- simulated here via `_record_gap` itself raising -- comes
    through unmodified.
    """
    session = open_ok(manager, pty)

    def exploding_record_gap(*args, **kwargs):
        raise RuntimeError("boom from _record_gap")

    session._record_gap = exploding_record_gap
    with pytest.raises(RuntimeError, match="boom from _record_gap"):
        session.scan_baud((9600, 115200), 0.1, no_op_decide)

    # And the session is not left wedged: the write lock and scan-pause
    # state were still released despite the exception.
    assert session._write_lock.acquire(timeout=1.0)
    session._write_lock.release()
    assert not session._scan_pause.is_set()


def test_gaps_overlapping_flags_only_gaps_inside_the_requested_window(manager, pty):
    session = open_ok(manager, pty)
    pty.send(b"pre-scan\r\n")
    until(lambda: session.buffer.head > 0, what="pre-scan capture")

    session.scan_baud((9600, 115200), 0.1, no_op_decide)
    at_cursor = session._gaps[0]["at_cursor"]
    assert at_cursor > 0, "the gap must start after the pre-scan bytes, not at 0"

    assert session.gaps_overlapping(0, at_cursor) != []
    assert session.gaps_overlapping(at_cursor, at_cursor + 1000) != []
    assert session.gaps_overlapping(at_cursor + 1, at_cursor + 1000) == []
    assert session.gaps_overlapping(0, at_cursor - 1) == []


# --------------------------------------------------------------------------
# Critical 2: a bad-but-positive candidate rate must not corrupt the line
# speed, must not skip the restore, and must not silently die a healthy
# session or escape tools.py as something other than SessionError.
# --------------------------------------------------------------------------

class _BaudRejectingSerial:
    """Wraps a real, already-open `serial.Serial`; setting `baudrate` to
    `fail_rate` APPLIES it for real first (so the fd genuinely ends up at a
    different, verifiable speed) and THEN raises `exc` -- reproducing
    pyserial's own custom-baud failure modes (ValueError from a
    kernel-rejected rate, OverflowError from the termios `array('i')` store
    for a rate >= 2**31) with real corrupted state for the restore to repair.

    A version that raised WITHOUT touching the real port first (this
    fixture's previous shape) only proves the exception path doesn't crash --
    it never exercises the restore at all, since there is nothing to restore.
    Confirmed on a pty: setting a custom (non-standard-table) rate really
    does change `termios.tcgetattr(fd)[5]` away from `termios.B9600`, and
    setting it back really does change it back -- this is not a Python-level
    cache, it is the kernel's own state for the fd.
    """

    def __init__(self, real, fail_rate, exc):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_fail_rate", fail_rate)
        object.__setattr__(self, "_exc", exc)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        real = object.__getattribute__(self, "_real")
        if name == "baudrate" and value == object.__getattribute__(self, "_fail_rate"):
            setattr(real, name, value)  # apply for real -- the corruption must exist
            raise object.__getattribute__(self, "_exc")
        setattr(real, name, value)


@pytest.mark.parametrize("exc", [ValueError("kernel rejected it"),
                                 OverflowError("Python int too large to convert to C int")])
def test_a_hardware_rejected_rate_is_recorded_and_the_scan_continues(manager, pty, exc):
    """Reproduces the review's finding against `rates=[9600, 2147483648, 115200]`:
    pyserial converts a kernel-rejected custom rate to ValueError, and the
    array('i') termios struct overflows for a rate >= 2**31.

    N4: this must NOT abort the whole scan (invariant 5 is ranked evidence,
    never a bare verdict) -- the other candidates are still sampled, and the
    rejection is named in the result rather than raised.

    Restore is verified against the REAL fd via `termios.tcgetattr`, not
    `serial.Serial.baudrate` -- that getter returns pyserial's own cached
    `_baudrate`, which the setter assigns BEFORE calling
    `_reconfigure_port()` (serialutil.py), so it reads back whatever was
    last ASKED for rather than what the kernel actually holds.
    """
    import termios

    session = open_ok(manager, pty, baud=9600)
    original = session._serial
    assert termios.tcgetattr(original.fd)[5] == termios.B9600
    session._serial = _BaudRejectingSerial(original, fail_rate=333333, exc=exc)
    try:
        result = session.scan_baud((9600, 333333, 115200), 0.1, no_op_decide)

        assert 333333 in result["rejected"]
        assert result["rejected"][333333] == str(exc)
        # The OTHER candidates were still sampled -- one bad rate did not
        # throw away the rest.
        assert set(result["samples"]) == {9600, 115200}

        # Restored on the REAL fd -- not merely a Python attribute that
        # claims 9600 while the kernel still holds the corrupted rate.
        assert termios.tcgetattr(original.fd)[5] == termios.B9600
        assert session.baud == 9600
        # A bad candidate value is not a device fault -- the session must
        # stay healthy.
        assert session.alive is True
        assert session.death_reason is None
        # The gap was still real: the reader really was paused.
        assert manager.status()["capture_gap_count"] == 1
    finally:
        session._serial = original

    # The port still works: capture resumes, and a later write goes through.
    pty.send(b"still healthy\r\n")
    until(lambda: b"still healthy" in session.buffer.read(0)[0],
          what="capture to resume after the restore")


def test_a_scan_notices_the_adapter_leaving_the_bus_on_a_quiet_line(manager, pty):
    """The quiet unplug, during a scan rather than during capture.

    `_read_forever` has always made this check; the scan's own read loop did
    not. A pty that is never written to produces empty samples at every
    candidate, which is byte-for-byte what a floating ground produces -- and
    tools.py hands that case a wiring hint. So without this check an adapter
    that is simply GONE draws "check the ground wire".

    The reader thread cannot be what notices: it is parked for the whole
    scan (`_scan_pause`), touching neither the port nor `os.path.lexists`.
    The test waits for that park before unlinking, so the only code that can
    set `death_reason` here is the scan's own loop.

    Would catch: the by-id check omitted from the scan loop (death_reason
    stays None and every sample comes back empty), or placed where a
    non-empty chunk would skip it.
    """
    session = open_ok(manager, pty, baud=9600)
    result: dict = {}

    def scan():
        result["value"] = session.scan_baud((9600, 115200, 230400), 0.5,
                                            no_op_decide)

    thread = threading.Thread(target=scan, daemon=True)
    thread.start()
    until(lambda: session._scan_parked.is_set(),
          what="the reader thread to park for the scan")

    pty.unlink_by_id()
    thread.join(timeout=DEADLINE)
    assert not thread.is_alive(), "the scan did not return after the unplug"

    value = result["value"]
    assert value["death_reason"] is not None, \
        "an unplugged adapter must not read back as a silent line"
    assert pty.by_id in value["death_reason"]
    assert "baud scan" in value["death_reason"]
    assert value["alive"] is False


def test_scan_baud_restores_the_original_rate_when_there_is_no_winner(manager, pty):
    session = open_ok(manager, pty, baud=9600)
    result = session.scan_baud((9600, 115200), 0.1, no_op_decide)

    assert result["final_baud"] == 9600
    assert result["restored"] is True
    assert session.baud == 9600
    assert session._serial.baudrate == 9600


def test_only_the_scanning_thread_calls_port_read_while_paused(manager, pty):
    """The deterministic version of the invariant below.

    Racing real bytes through a pty is not a reliable discriminator either
    way (see the next test's docstring) -- a race can go either way run to
    run. WHO calls `port.read()` while `_scan_pause` is set is not racy at
    all: it does not depend on any byte actually arriving, only on which
    thread is making the call, which is exactly the mechanism the pause
    handshake exists to control.
    """
    session = open_ok(manager, pty)
    real_read = session._serial.read
    callers_while_paused: list[threading.Thread] = []
    lock = threading.Lock()

    def spy(*args, **kwargs):
        # Gated on BOTH flags, not just `_scan_pause`: the reader checks
        # `_scan_pause` and calls `port.read()` as two separate statements
        # (`_read_forever`), so there is a real window, between the scan
        # setting `_scan_pause` and the reader finishing its own in-flight
        # read from BEFORE the scan even started, where `_scan_pause` is set
        # but the reader has not parked yet. A predicate on `_scan_pause`
        # alone records that entirely legitimate last read as a false
        # "culprit" -- confirmed by widening the window with a sleep here,
        # which produced failures against known-correct code every time
        # until this gate was added. `_scan_parked` being set is what
        # actually means "the scan believes it holds the port alone".
        if session._scan_pause.is_set() and session._scan_parked.is_set():
            with lock:
                callers_while_paused.append(threading.current_thread())
        return real_read(*args, **kwargs)

    session._serial.read = spy

    scan_thread: dict = {}

    def run_scan():
        scan_thread["value"] = threading.current_thread()
        session.scan_baud((9600, 115200, 230400), 0.15, no_op_decide)

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    thread.join(DEADLINE)

    assert callers_while_paused, "no read() calls observed during the scan; proves nothing"
    culprits = {t.name for t in callers_while_paused if t is not scan_thread["value"]}
    assert not culprits, (
        f"port.read() was called by {culprits} while the scan believed it "
        "held the port alone -- the reader thread did not actually park"
    )


def test_scan_baud_samples_never_reach_the_capture_buffer(manager, pty):
    """The invariant the whole coordination design exists for.

    NOT provable with a single racy send: the scan's first `reset_input_buffer()`
    can destroy a one-shot marker before either thread would have seen it,
    which makes a post-hoc `head == 0` check pass whether or not the pause
    actually held -- confirmed by mutating `_read_forever` to ack the pause
    and then keep reading and appending anyway: of three runs, one passed
    outright and the other two failed only on the secondary "did the marker
    arrive in samples" assertion, never on `head == 0` itself.

    Sending CONTINUOUSLY for the whole scan and polling `buffer.head`
    CONTINUOUSLY removes the race: a mutant reader that keeps appending has
    the whole scan duration, not one instant, to be caught growing `head`,
    and the property asserted (head never exceeds its value at the moment
    the reader parked) does not depend on who wins any single read.
    """
    session = open_ok(manager, pty, baud=9600)

    stop_sender = threading.Event()

    def sender():
        while not stop_sender.is_set():
            try:
                pty.send(b"x" * 64)
            except OSError:
                return
            time.sleep(0.005)

    sender_thread = threading.Thread(target=sender, daemon=True)
    sender_thread.start()

    scan_result: dict = {}
    scan_done = threading.Event()

    def run_scan():
        scan_result["value"] = session.scan_baud(
            (9600, 115200, 230400), 0.3, no_op_decide)
        scan_done.set()

    scanner_thread = threading.Thread(target=run_scan, daemon=True)
    scanner_thread.start()
    until(lambda: session._scan_parked.is_set(), what="the reader to park")
    # Safe to read: the reader's own last append (if any) happened-before it
    # set `_scan_parked`, so this is a true upper bound from this instant on.
    head_at_park = session.buffer.head

    observed_heads = []
    while not scan_done.is_set():
        observed_heads.append(session.buffer.head)
        time.sleep(0.01)

    stop_sender.set()
    sender_thread.join(DEADLINE)
    scanner_thread.join(DEADLINE)

    assert observed_heads, "the poll loop never ran; this test proves nothing"
    assert max(observed_heads) == head_at_park, (
        f"buffer.head grew from {head_at_park} to {max(observed_heads)} while "
        "the scan believed it was the only thing reading the port"
    )
    # NOT `assert session.buffer.head == head_at_park` here: by this point
    # `scan_baud` has already returned, which means `_scan_pause` is already
    # clear and the reader has already resumed -- correctly. Bisected: 0ms
    # and 20ms of slack after `scan_done` passed, 40ms and 250ms failed with
    # `head` genuinely grown (128 -> 256), because the resumed reader's own
    # next read legitimately appended more of the continuous sender's bytes.
    # That is correct behaviour, not the violation this test exists to
    # catch -- asserting on it here tests a coincidence of timing, not the
    # invariant. `max(observed_heads)` above is the sound check: every value
    # in it was sampled from inside the `while not scan_done.is_set()` loop,
    # i.e. while the scan was still actually running.

    total_sampled = sum(len(v) for v in scan_result["value"]["samples"].values())
    assert total_sampled > 0, (
        "the continuous sender never got any bytes into the scan's own "
        "samples either -- this test would prove nothing"
    )


def test_reader_resumes_capturing_after_the_scan_ends(manager, pty):
    session = open_ok(manager, pty)
    session.scan_baud((9600, 115200), 0.1, no_op_decide)

    pty.send(b"post-scan capture\r\n")
    until(lambda: b"post-scan capture" in session.buffer.read(0)[0],
          what="the reader to resume once the scan hands the port back")


def test_reader_resumes_at_the_winning_rate_after_the_scan_ends(manager, pty):
    session = open_ok(manager, pty, baud=9600)
    session.scan_baud((9600, 115200), 0.1, lambda s: 115200)
    assert session._serial.baudrate == 115200

    pty.send(b"post-scan at new rate\r\n")
    until(lambda: b"post-scan at new rate" in session.buffer.read(0)[0],
          what="the reader to keep capturing at the rate the scan left it on")


def test_a_concurrent_write_waits_for_the_scan_rather_than_racing_it(manager, pty):
    """Holding `_write_lock` for the whole scan, not just the reconfiguration,
    is what stops a wrong-rate write from reaching the target mid-scan."""
    session = open_ok(manager, pty)
    scan_done = threading.Event()
    write_saw_scan_done = []
    write_finished = threading.Event()

    def run_scan():
        session.scan_baud((9600, 115200, 230400), 0.2, no_op_decide)
        scan_done.set()

    def run_write():
        session.write(b"x")
        write_saw_scan_done.append(scan_done.is_set())
        write_finished.set()

    scanner = threading.Thread(target=run_scan, daemon=True)
    scanner.start()
    until(lambda: session._scan_parked.is_set(), what="the scan to start")

    writer = threading.Thread(target=run_write, daemon=True)
    writer.start()
    assert write_finished.wait(DEADLINE)
    assert write_saw_scan_done == [True], \
        "the write proceeded while the scan still held the port"

    scanner.join(DEADLINE)
    writer.join(DEADLINE)


def test_a_send_waiting_on_the_scan_lock_does_not_stall_other_tool_calls(manager, pty, monkeypatch):
    """The whole-worker version of the starvation fix, over the real lock.

    Task 7 moved `scan_baud` off the event loop "so a concurrent low-tier
    call (status, a read) is not starved for the whole scan". That is true of
    the scan and was false of the worker: `console_send` waits on the same
    `_write_lock` the scan holds, and `console_send` was inline, so the
    starvation simply moved to whoever called it. Measured through the real
    handlers over this pty before the fix: 3.78s blocked, 0 of the ~75 status
    polls due in that window completed.

    This test lives in test_session.py rather than with the other tools tests
    because it needs the pty fixtures and a real `SessionManager` -- the fake
    in test_tools_concurrency.py pins the same property deterministically,
    while this one pins it against the actual lock the scan holds.

    Would catch `sess.write(payload)` being re-inlined in tools.py: the probe
    gets no control for the whole wait and ticks zero times.
    """
    import asyncio
    import base64
    import json

    from pare_hardware_mcp import tools

    monkeypatch.setattr(tools, "MANAGER", manager)
    session = open_ok(manager, pty, baud=9600)

    scan = threading.Thread(
        target=lambda: session.scan_baud((9600, 115200, 230400), 0.5,
                                         no_op_decide),
        daemon=True)
    scan.start()
    until(lambda: session._scan_parked.is_set(),
          what="the scan to take _write_lock and park the reader")

    async def drive():
        done = asyncio.Event()
        ticks = {"n": 0}

        async def probe():
            while not done.is_set():
                # A real low-tier tool call, not a bare sleep: the claim is
                # that console_status still ANSWERS during the wait.
                json.loads(await tools.console_status())
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(probe())
        try:
            raw = await tools.console_send(
                session=session.id,
                data_b64=base64.b64encode(b"reboot\r").decode())
        finally:
            done.set()
            await task
        return json.loads(raw), ticks["n"]

    out, ticks = asyncio.run(drive())
    scan.join(timeout=DEADLINE)

    assert out.get("sent") == 7, out
    # The send genuinely waits for the scan -- that is the design, and it is
    # not what this test objects to. What it pins is that everything else
    # kept answering while it waited.
    assert ticks >= 10, f"console_status was starved during the send: {ticks}"


def test_close_during_a_scan_aborts_it_promptly_rather_than_hanging(manager, pty):
    session = open_ok(manager, pty)
    scan_returned = threading.Event()

    def run_scan():
        try:
            session.scan_baud((9600, 115200, 230400, 460800), 3.0, no_op_decide)
        except SessionError:
            pass
        finally:
            scan_returned.set()

    scanner = threading.Thread(target=run_scan, daemon=True)
    scanner.start()
    until(lambda: session._scan_parked.is_set(), what="the scan to start")

    started = time.monotonic()
    manager.close(session.id)
    elapsed = time.monotonic() - started

    assert elapsed < WRITE_DRAIN_TIMEOUT, \
        f"close took {elapsed:.2f}s waiting on a scan that should abort promptly"
    assert scan_returned.wait(DEADLINE)
    scanner.join(DEADLINE)
    assert manager.current is None


# --------------------------------------------------------------------------
# The SWEEP. The bench defect: `scan_baud` made one long pass per candidate,
# which is correct against a line that keeps talking and wrong against the
# target that found it -- a board that emits a burst at boot and then goes
# silent. Whichever candidate held the window when the burst arrived was the
# only rate to see a byte, so it won by luck.
#
# A pty cannot reframe bytes by baud rate, so these tests cannot show the
# CORRECT rate winning. What they can show, and what the defect actually
# was, is the SHAPE of the time: that a short burst reaches every candidate
# rather than exactly one, that every candidate is sampled before an early
# exit may be taken, and that the budget still bounds the whole thing.
# --------------------------------------------------------------------------

def burst_after(pty, delay: float, payload: bytes, stop: threading.Event):
    """Play a board that speaks once, some time after the scan starts."""
    def run():
        if stop.wait(delay):
            return
        pty.send(payload)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_short_burst_reaches_every_candidate_rate_not_just_one(manager, pty):
    """THE defect-2 regression.

    The burst is 0.35s long. One full sweep of three candidates at 0.1s
    dwell is 0.3s, so a sweeping scan puts part of the burst in every rate's
    window. The single-pass scan this replaced spent 0.5s per candidate over
    the same 1.5s budget, so a 0.35s burst fell entirely inside ONE
    candidate's window and every other rate reported silence.

    Would catch: the sweep collapsed back to one pass (verified -- with the
    loop reverted to a single pass at the same total budget, exactly one
    rate receives bytes and `sweeps` is 1).
    """
    session = open_ok(manager, pty, baud=9600)
    stop = threading.Event()
    payload = b"U-Boot 2017.09\r\nDRAM:  4 GiB\r\n" * 12
    try:
        # Emit continuously for ~0.35s starting shortly after the scan does.
        def run():
            if stop.wait(0.15):
                return
            end = time.monotonic() + 0.35
            while time.monotonic() < end and not stop.is_set():
                pty.send(payload)
                time.sleep(0.02)
        writer = threading.Thread(target=run, daemon=True)
        writer.start()
        result = session.scan_baud((9600, 115200, 230400), 0.1, no_op_decide,
                                   budget_seconds=1.5)
    finally:
        stop.set()
    writer.join(timeout=DEADLINE)

    assert result["sweeps"] >= 3, (
        f"a 1.5s budget at 0.1s x 3 rates must sweep several times, not "
        f"{result['sweeps']}")
    heard = {rate for rate, data in result["samples"].items() if data}
    assert heard == {9600, 115200, 230400}, (
        "a burst shorter than a single-pass dwell must still reach every "
        f"candidate; only {heard} heard anything")


def test_the_scan_stays_inside_its_budget_while_sweeping(manager, pty):
    """Sweeping changes the shape of the time spent, not the amount.

    Would catch a sweep loop with no deadline check, which runs forever
    against a talking line, and -- the mutation that survived the first
    version of this test -- one that checks the deadline only BETWEEN
    sweeps, which overruns by a whole sweep.

    The bound is stated as a relationship rather than a number, because the
    number is the thing being tested: the deadline is checked before each
    CANDIDATE, so the overrun is at most the dwell in flight, and that is
    strictly less than half a sweep for any list of more than two rates. A
    between-sweeps check overruns by a full sweep and fails it. The rates are
    chosen so one sweep is comparable to the whole budget, which is what
    makes the two cases far apart in wall time (~1.35s against ~2.0s) rather
    than a few milliseconds apart.
    """
    session = open_ok(manager, pty, baud=9600)
    stop = threading.Event()
    pump = threading.Thread(
        target=lambda: [pty.send(b"chatter chatter\r\n") or time.sleep(0.005)
                        for _ in iter(lambda: not stop.is_set(), False)],
        daemon=True)
    pump.start()
    rates = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600,
             1152000, 1500000)
    dwell, budget = 0.1, 1.2
    try:
        started = time.monotonic()
        result = session.scan_baud(rates, dwell, no_op_decide,
                                   budget_seconds=budget)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
    pump.join(timeout=DEADLINE)

    one_sweep = len(rates) * dwell
    assert result["sweeps"] >= 1
    assert elapsed < budget + one_sweep / 2, (
        f"overran the {budget}s budget by more than half a sweep: {elapsed}s")
    assert result["budget_seconds"] == budget


def test_every_candidate_is_sampled_before_an_early_exit_may_be_taken(manager, pty):
    """An early exit must not make the result dishonest about rates it never
    reached.

    `early_exit` here accepts the very first thing it is offered, which is
    the most aggressive exit possible. Every candidate must still appear in
    `samples` with its own `listen_seconds`, because the exit is only
    offered at the END of a completed sweep.

    Would catch the early-exit check moved inside the candidate loop, which
    is the obvious place to put it and which drops every later rate from the
    ranking without saying so.
    """
    session = open_ok(manager, pty, baud=9600)
    stop = threading.Event()
    pump = threading.Thread(
        target=lambda: [pty.send(b"talking\r\n") or time.sleep(0.005)
                        for _ in iter(lambda: not stop.is_set(), False)],
        daemon=True)
    pump.start()
    try:
        rates = (9600, 115200, 230400, 460800)
        result = session.scan_baud(rates, 0.1, no_op_decide,
                                   budget_seconds=5.0,
                                   early_exit=lambda data: True)
    finally:
        stop.set()
    pump.join(timeout=DEADLINE)

    assert result["early_exit"] is True
    assert result["sweeps"] == 1, "the exit is taken at the end of sweep 1"
    assert set(result["samples"]) == set(rates), (
        "an early exit left candidates unsampled: "
        f"{set(rates) - set(result['samples'])}")
    for rate in rates:
        assert result["listen_seconds"][rate] > 0.0, rate


def test_an_early_exit_that_never_fires_spends_the_whole_budget(manager, pty):
    """The companion: `early_exit` returning False must not end the scan.

    Would catch the predicate's sense inverted, and an exit taken on a
    falsy return.

    The line has to be talking for this to mean anything: an EMPTY sample is
    never offered to the predicate -- silence cannot look like console output
    at any rate, and scoring it would only give a buggy predicate a chance to
    end the scan on no evidence at all.
    """
    session = open_ok(manager, pty, baud=9600)
    stop = threading.Event()
    pump = threading.Thread(
        target=lambda: [pty.send(b"talking\r\n") or time.sleep(0.005)
                        for _ in iter(lambda: not stop.is_set(), False)],
        daemon=True)
    pump.start()
    calls = []
    try:
        result = session.scan_baud((9600, 115200), 0.1, no_op_decide,
                                   budget_seconds=0.8,
                                   early_exit=lambda data: calls.append(1) and False)
    finally:
        stop.set()
    pump.join(timeout=DEADLINE)
    assert result["early_exit"] is False
    assert result["sweeps"] >= 3
    assert calls, "the predicate was never consulted"


def test_a_default_budget_is_exactly_one_sweep(manager, pty):
    """The single-pass shape survives as the degenerate case of the sweep,
    so the coordination tests above it exercise the same code path rather
    than a second one that could drift.
    """
    session = open_ok(manager, pty, baud=9600)
    result = session.scan_baud((9600, 115200, 230400), 0.1, no_op_decide)
    assert result["budget_seconds"] == pytest.approx(0.3)
    assert result["sweeps"] == 1
    assert set(result["samples"]) == {9600, 115200, 230400}


def test_a_rate_the_hardware_refuses_is_not_retried_on_every_sweep(manager, pty):
    """A refused rate is refused for the whole scan, not once per pass.

    Sweeping turns one wasted termios call into one per sweep, and -- worse
    -- `rejected[rate]` would be rewritten each time, so a transient refusal
    on a later sweep could overwrite the first and truthful reason. Would
    catch the rejected rate left in the sweep's working list.
    """
    session = open_ok(manager, pty, baud=9600)
    attempts = []
    real = type(session._serial).baudrate

    class Spy:
        def __get__(self, obj, owner=None):
            return real.__get__(obj, owner)

        def __set__(self, obj, value):
            attempts.append(value)
            if value == 333333:
                raise ValueError("kernel refused it")
            real.__set__(obj, value)

    monkey = type(session._serial)
    original = monkey.baudrate
    monkey.baudrate = Spy()
    try:
        result = session.scan_baud((9600, 333333, 115200), 0.1, no_op_decide,
                                   budget_seconds=1.0)
    finally:
        monkey.baudrate = original

    assert result["sweeps"] >= 3
    assert result["rejected"] == {333333: "kernel refused it"}
    assert attempts.count(333333) == 1, (
        f"the refused rate was re-applied {attempts.count(333333)} times "
        f"across {result['sweeps']} sweeps")
    assert 333333 not in result["samples"]


def test_a_sweep_still_records_exactly_one_capture_gap(manager, pty):
    """The reader parks ONCE for the whole sweep, not once per dwell.

    This is the part of the change that had to not move: `_scan_pause` /
    `_scan_parked` and the gap ledger are unchanged, and a sweep of many
    dwells must still read as one suspension. Would catch a sweep that
    parked and unparked the reader per pass, which would also mean the
    reader appending bytes into the capture at a candidate rate.
    """
    session = open_ok(manager, pty, baud=9600)
    pty.send(b"before the scan\r\n")
    until(lambda: session.buffer.head > 0, what="pre-scan capture")

    before = session.gap_summary()["count"]
    result = session.scan_baud((9600, 115200, 230400), 0.1, no_op_decide,
                               budget_seconds=1.0)
    after = session.gap_summary()

    assert result["sweeps"] >= 2
    assert after["count"] == before + 1, "one scan, one gap, however many sweeps"
    assert after["capture_suspended"] is False
    assert result["gap"]["duration_s"] >= 1.0


def test_a_multi_sweep_scan_keeps_the_reader_parked_the_whole_time(manager, pty):
    """The single highest-risk thing the sweep could have broken.

    `test_scan_baud_samples_never_reach_the_capture_buffer` proves the same
    property, but it runs a ONE-SWEEP scan -- so a sweep that releases and
    re-takes the park between passes has no "between" for it to be caught
    in, and it passes against that mutation. Verified: with the sweep
    mutated to clear `_scan_pause` and re-park each pass, the whole suite
    passed and only this test fails.

    It matters because an unparked reader does not merely race for bytes: it
    appends them to the CAPTURE, at whatever candidate rate the scan has
    applied at that instant, as if the target had said them at the session's
    real rate. The park is what makes repeated sweeping safe at all.

    Method is the existing test's, for the same reason: send continuously
    and poll continuously, so a mutant reader has the whole scan to be
    caught growing `head` rather than one racy instant.
    """
    session = open_ok(manager, pty, baud=9600)
    stop_sender = threading.Event()

    def sender():
        while not stop_sender.is_set():
            try:
                pty.send(b"y" * 64)
            except OSError:
                return
            time.sleep(0.005)

    sender_thread = threading.Thread(target=sender, daemon=True)
    sender_thread.start()

    scan_result: dict = {}
    scan_done = threading.Event()

    def run_scan():
        try:
            scan_result["value"] = session.scan_baud(
                (9600, 115200, 230400), 0.1, no_op_decide, budget_seconds=1.2)
        finally:
            scan_done.set()

    scanner = threading.Thread(target=run_scan, daemon=True)
    scanner.start()
    until(lambda: session._scan_parked.is_set(), what="the reader to park")
    head_at_park = session.buffer.head

    observed_heads = []
    unparked = []
    while not scan_done.is_set():
        observed_heads.append(session.buffer.head)
        if not session._scan_pause.is_set():
            unparked.append(time.monotonic())
        time.sleep(0.005)

    stop_sender.set()
    sender_thread.join(DEADLINE)
    scanner.join(DEADLINE)

    assert observed_heads, "the poll loop never ran; this test proves nothing"
    assert scan_result["value"]["sweeps"] >= 3, (
        "a one-sweep scan cannot exercise the between-sweeps window this "
        f"test exists for (swept {scan_result['value'].get('sweeps')})")
    assert unparked == [], (
        f"the scan released the reader park {len(unparked)} time(s) mid-scan")
    assert max(observed_heads) == head_at_park, (
        f"buffer.head grew from {head_at_park} to {max(observed_heads)} while "
        "the scan believed it was the only thing reading the port")
    assert sum(len(v) for v in scan_result["value"]["samples"].values()) > 0, (
        "the sender never reached the scan's own samples either -- this "
        "test would prove nothing")


def test_the_first_sweep_completes_even_when_the_budget_is_already_spent(manager, pty):
    """The honesty guarantee, at its boundary.

    `check_budget` refuses a candidate list too long to sweep once, but
    `scan_baud` takes `budget_seconds` from its caller and must not rely on
    that check having been made: every candidate gets sampled at least once
    or the ranking is silent about rates nobody ever tried. A budget smaller
    than a single dwell is the sharpest form of the question.

    Would catch the first-sweep exemption dropped from the deadline check --
    which is a one-token change (`if sweeps >= 1 and ...` to `if ...`) and
    leaves a scan reporting one candidate out of four with no indication the
    others were skipped.
    """
    rates = (9600, 115200, 230400, 460800)
    session = open_ok(manager, pty, baud=9600)
    result = session.scan_baud(rates, 0.05, no_op_decide, budget_seconds=0.001)

    assert result["sweeps"] == 1
    assert set(result["samples"]) == set(rates), (
        "candidates were skipped by an already-spent budget: "
        f"{set(rates) - set(result['samples'])}")
    for rate in rates:
        assert result["listen_seconds"][rate] > 0.0, rate
