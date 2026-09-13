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
import select
import threading
import time

import pytest
import serial

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
