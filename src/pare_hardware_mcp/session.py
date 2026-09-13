"""One console session: owning the port, capturing from the instant it opens.

Three things make this module harder than it looks, and each one is a design
decision rather than an implementation detail.

**Capture starts inside `open()`, not on the first `read()`.** The boot log is
the artifact a hardware investigation exists to get, and it arrives in the
first few seconds -- before anyone has a question to ask, let alone a cursor
to ask with. So `open()` starts a background reader thread and does not return
until that thread is running. `read` is then served from `CaptureBuffer`, never
from the device: a tool call is not allowed to wait on a UART that may say
nothing for an hour.

**A device that vanishes is a named failure, not silence.** A USB adapter
pulled mid-boot gives two distinct symptoms, and both must be caught. The loud
one is a read error (EIO, or pyserial's "device reports readiness to read but
returned no data"). The quiet one is a line that simply stops -- reads time out
empty, which is indistinguishable from a target that has nothing to say. The
reader therefore also checks that the `by-id` path still resolves: `udev`
removes that symlink on unplug, so its absence is positive evidence of a
disappearance rather than an inference from silence. Either way the session is
marked not alive with a reason naming the path, and the capture stays readable.

**`close` is the only thing that ends a session.** Not a read error, not the
unplug, not the daemon's TCP connection dropping -- this bench sits behind a
tailnet that blips, and throwing away a boot log because a socket closed would
discard exactly what the bench exists to collect. A dead session keeps the slot
and keeps its bytes; a reconnecting daemon calls `get()` and adopts it.

**Locking.** `CaptureBuffer` ships with no locking by design (see its module
docstring). This module supplies it: `_LockedCaptureBuffer` wraps `append` and
`read` in one `threading.Lock`, so the single reader thread and any number of
tool-call threads contend on that lock and nothing else. The critical sections
are pure memory moves -- no I/O, no blocking -- so a tool call waits microseconds
at worst, which is what invariant 8 needs. The manager's own `_lock` guards the
one-session slot and is never held by the reader thread, so the two can never
deadlock against each other.

**What a pty cannot tell you.** DTR and RTS are set explicitly here (pyserial's
default asserts both, and many boards reset on DTR), but the POSIX `open(2)`
that precedes any ioctl asserts the modem lines itself on a real UART. Nothing
in userspace can lower them before that happens, so this module's promise is
"the state you asked for is the state the port is left in, and it is reported
back" -- not "your board will not reset".
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import serial

from .devices import ResolvedDevice, resolve_device
from .ringbuffer import DEFAULT_CAPACITY, CaptureBuffer

# How long a single `read()` on the port may block before the reader loop gets
# control back to check for a stop request or a vanished device. Short enough
# that `close` and unplug detection feel immediate, long enough that an idle
# line costs ~20 wake-ups a second on a Pi.
READ_TIMEOUT = 0.05
READ_CHUNK = 4096

# `close` asks the reader to stop and unblocks its `select` with
# `cancel_read()`; a loop iteration is bounded by READ_TIMEOUT, so this is two
# orders of magnitude of slack. Exceeding it means the reader is wedged
# somewhere it should not be, and close proceeds anyway rather than hanging the
# worker -- the operator must always be able to let go of the port.
JOIN_TIMEOUT = 2.0

# `close` must not pull the fd out from under an in-flight `write`, so it waits
# for `_write_lock`. `write` is bounded by the port's `write_timeout` (5s below)
# and `cancel_write()` aborts it sooner, so this is slack on a bound that should
# already have fired -- it exists only so that a `write_timeout` of None could
# never leave the operator unable to let go of the port.
WRITE_DRAIN_TIMEOUT = 6.0

FLOW_MODES = ("none", "rtscts", "xonxoff")


class SessionError(RuntimeError):
    """A session could not be opened, or an open session refused an operation."""


class _LockedCaptureBuffer(CaptureBuffer):
    """`CaptureBuffer` plus the lock it deliberately does not ship with.

    One writer (the reader thread) and any number of readers (tool calls).
    `head` is left unlocked on purpose: it is a single integer assignment made
    *after* the bytes are in place, so a concurrent reader sees either the old
    value or the new one and both are consistent snapshots.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__(capacity)
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            super().append(data)

    def read(self, cursor: int, limit: int | None = None):
        with self._lock:
            return super().read(cursor, limit)


class ConsoleSession:
    """One open console: a port, a reader thread, and everything it captured."""

    def __init__(self, *, session_id: str, device: ResolvedDevice, port: serial.Serial,
                 baud: int, flow: str, dtr: bool, rts: bool | None,
                 capacity: int = DEFAULT_CAPACITY) -> None:
        self.id = session_id
        self.device = device
        self.baud = baud
        self.flow = flow
        self.dtr = dtr
        # `None` means "hardware flow control owns this line" -- see
        # `SessionManager._open_port`. Reporting the value we asked for when
        # the driver overrode it would be a false report of a line that resets
        # boards.
        self.rts: bool | None = rts
        self.opened_at = time.time()          # wall clock, for reporting
        self._opened_monotonic = time.monotonic()  # for age; immune to clock steps

        self._serial = port
        self._buffer: CaptureBuffer | None = _LockedCaptureBuffer(capacity)
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._alive = True
        self._death_reason: str | None = None
        self._closed = False
        self._stop = threading.Event()
        self._reader = threading.Thread(
            target=self._read_forever,
            name=f"console-reader-{session_id}",
            daemon=True,
        )

    # -- state ------------------------------------------------------------

    @property
    def buffer(self) -> CaptureBuffer:
        """The capture. Available until `close`, which discards it.

        Reading a closed session's buffer is a caller bug, and a loud one: the
        alternative is handing back a capture belonging to a session that is
        over, which a language model would happily reason about as if it were
        current.
        """
        buffer = self._buffer
        if buffer is None:
            raise SessionError(
                f"session {self.id} is closed and its capture was discarded"
            )
        return buffer

    @property
    def alive(self) -> bool:
        with self._state_lock:
            return self._alive

    @property
    def death_reason(self) -> str | None:
        with self._state_lock:
            return self._death_reason

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._opened_monotonic

    def _die(self, reason: str) -> None:
        """Mark the link dead. The first reason wins -- it is the cause."""
        with self._state_lock:
            if self._death_reason is None:
                self._death_reason = reason
            self._alive = False

    def _explain(self, exc: BaseException, during: str) -> str:
        """Name the failure, and say whether the device itself is gone.

        `udev` deletes the by-id symlink on unplug, so a path that no longer
        resolves is evidence of a disappearance rather than a guess at one.
        """
        if not os.path.lexists(self.device.by_id):
            return (
                f"device {self.device.by_id} disappeared during {during}: the "
                f"by-id path no longer resolves (was {self.device.tty}) "
                f"[{type(exc).__name__}: {exc}]"
            )
        return (
            f"{during} failed on {self.device.by_id} (tty {self.device.tty}): "
            f"{type(exc).__name__}: {exc}"
        )

    # -- the reader -------------------------------------------------------

    def _read_forever(self) -> None:
        """Drain the port into the buffer until told to stop or the device dies.

        Holds its own reference to the buffer so that a `close` racing the last
        iteration appends into an orphaned buffer instead of tripping over
        `None`.
        """
        buffer = self._buffer
        assert buffer is not None
        port = self._serial
        while not self._stop.is_set():
            try:
                data = port.read(READ_CHUNK)
            except (serial.SerialException, OSError) as exc:
                if self._stop.is_set():
                    return  # close() pulled the port out from under us; not a death
                self._die(self._explain(exc, "read"))
                return
            except BaseException as exc:  # noqa: BLE001
                # A bug in here must not leave a session that looks alive and
                # captures nothing -- that is the silent failure invariant 5
                # exists to prevent.
                self._die(self._explain(exc, "reader thread"))
                return

            if data:
                buffer.append(data)
                continue

            # A quiet line. Distinguish "target has nothing to say" from
            # "adapter is no longer on the bus".
            if not os.path.lexists(self.device.by_id):
                self._die(
                    f"device {self.device.by_id} disappeared: the by-id path no "
                    f"longer resolves (was {self.device.tty}) and the line has "
                    "gone quiet"
                )
                return

    # -- operations -------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Put bytes on the line. Refuses rather than writing into the void.

        The liveness checks are inside `_write_lock`, not before it: outside,
        they are check-then-act, and `_shutdown` can close the port in the gap
        between the check passing and the write starting.
        """
        with self._write_lock:
            if self._closed:
                raise SessionError(f"session {self.id} is closed")
            if not self.alive:
                raise SessionError(
                    f"session {self.id} is not alive, refusing to write: "
                    f"{self.death_reason}"
                )
            try:
                # No `flush()`. `write` is bounded by `write_timeout`, but
                # `flush` is `tcdrain(2)` and has no timeout at all: under
                # rtscts against a target that never asserts CTS it blocks the
                # tool call for as long as the target stays silent. The bytes
                # are in the kernel's tty buffer either way.
                self._serial.write(data)
            except serial.SerialTimeoutException as exc:
                # NOT a death. `SerialTimeoutException` subclasses
                # `SerialException` (serialutil.py:96), so lumping it in with
                # the branch below would mark a perfectly healthy session dead
                # and permanently: `_alive` is only ever set True in
                # `__init__`. The target that does not assert CTS under
                # flow="rtscts" -- the case invariant 4 exists for -- would
                # cost one timeout and then write-brick the console for the
                # rest of the session, while `status()` claimed the device was
                # gone and the reader carried on capturing from it. A timeout
                # is a bounded, recoverable refusal by the far end.
                raise SessionError(
                    f"write on session {self.id} timed out after "
                    f"{self._serial.write_timeout}s: the target is not "
                    f"accepting bytes (flow={self.flow}"
                    + ("; under rtscts that is a target not asserting CTS"
                       if self.flow == "rtscts" else "")
                    + "). The session is still open and the capture is "
                      "unaffected."
                ) from exc
            except (serial.SerialException, OSError) as exc:
                self._die(self._explain(exc, "write"))
                raise SessionError(
                    f"write on session {self.id} failed: {self.death_reason}"
                ) from exc

    def _shutdown(self) -> str | None:
        """Stop the reader, release the port, discard the capture.

        Only `SessionManager.close` calls this. Returns a warning string if the
        reader would not join, so the caller can surface it rather than have it
        vanish.
        """
        warnings: list[str] = []
        self._stop.set()
        for cancel in (self._serial.cancel_read, self._serial.cancel_write):
            try:
                cancel()
            except Exception:  # noqa: BLE001 -- best effort; the bounds below cover us
                pass
        if self._reader.is_alive():
            self._reader.join(timeout=JOIN_TIMEOUT)
            if self._reader.is_alive():
                warnings.append(
                    f"reader thread for session {self.id} did not stop within "
                    f"{JOIN_TIMEOUT}s; releasing the port anyway"
                )

        # The fd must not be closed under an in-flight `write`. pyserial's
        # `write` re-reads `self.fd` on every loop iteration
        # (serialposix.py:621) while `close` sets it to None and closes it
        # unconditionally (serialposix.py:529-541). Close it underneath a
        # blocked writer and two things follow: `os.write(None, ...)` raises a
        # bare TypeError straight out of `write()` past both except clauses,
        # and -- worse -- the next `open` can be handed the same fd number, so
        # this session's bytes are transmitted to the *next* session's physical
        # target. The realistic way a write stays in flight that long is the
        # CTS-low stall this module is built around, with console_send and
        # console_close arriving as concurrent tool calls.
        acquired = self._write_lock.acquire(timeout=WRITE_DRAIN_TIMEOUT)
        try:
            if not acquired:
                warnings.append(
                    f"a write on session {self.id} was still in flight after "
                    f"{WRITE_DRAIN_TIMEOUT}s; releasing the port anyway"
                )
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001 -- an unplugged fd fails to close;
                pass           # the session is over and must not hang here
            with self._state_lock:
                self._alive = False
            # Set inside `_write_lock` so a `write` waiting on it sees a closed
            # session the moment it gets in, rather than writing to a dead fd.
            self._closed = True
            self._buffer = None
        finally:
            if acquired:
                self._write_lock.release()
        return "; ".join(warnings) if warnings else None

    def describe(self) -> dict:
        """The session half of `status()`. Every value derived, none cached."""
        buffer = self._buffer
        head = buffer.head if buffer is not None else None
        capacity = buffer.capacity if buffer is not None else None
        # Bytes evicted by wraparound so far: derived from head and capacity so
        # it cannot drift out of step with them.
        dropped = None if buffer is None else max(0, head - capacity)
        with self._state_lock:
            alive, death_reason = self._alive, self._death_reason
        return {
            "open": True,
            "session": self.id,
            "device": self.device.by_id,
            "tty": self.device.tty,
            "serial": self.device.serial,
            "interface": self.device.interface,
            "baud": self.baud,
            "flow": self.flow,
            "dtr": self.dtr,
            "rts": self.rts,
            "opened_at": self.opened_at,
            "age_s": round(self.age_s, 3),
            "alive": alive,
            "death_reason": death_reason,
            "buffer_head": head,
            "buffer_capacity": capacity,
            "dropped": dropped,
        }


class SessionManager:
    """Holds the one console session this worker is allowed to have.

    One session per worker, because there is one operator and one bench: a
    second claim on the port is a mistake to report, not a queue to join. The
    slot is held until `close`, alive or dead.
    """

    STATUS_KEYS = (
        "open", "session", "device", "tty", "serial", "interface", "baud",
        "flow", "dtr", "rts", "opened_at", "age_s", "alive", "death_reason",
        "buffer_head", "buffer_capacity", "dropped",
    )

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._current: ConsoleSession | None = None
        self._lock = threading.RLock()
        self.last_close_warning: str | None = None

    @property
    def current(self) -> ConsoleSession | None:
        with self._lock:
            return self._current

    def open(self, *, device: str, expect_serial: str | None, baud: int,
             flow: str = "none", dtr: bool, rts: bool) -> ConsoleSession:
        """Claim the port and start capturing before returning.

        `flow` defaults to none (invariant 4); `dtr` and `rts` have no defaults
        on purpose (invariant 3) -- the caller must say what happens to lines
        that reset many boards. The one case where the answer is not the
        caller's: under `flow="rtscts"` the driver drives RTS, so the session
        reports `rts=None` rather than echoing back a value it did not set.

        Raises `DeviceError` if the path or the declared serial does not
        resolve, and `SessionError` for a bad argument, a port already held, or
        a session already open.
        """
        if flow not in FLOW_MODES:
            raise SessionError(
                f"unknown flow control {flow!r}; expected one of "
                f"{', '.join(FLOW_MODES)}"
            )
        if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
            raise SessionError(f"baud must be a positive integer, got {baud!r}")

        with self._lock:
            existing = self._current
            if existing is not None:
                raise SessionError(
                    f"session {existing.id} is already open on "
                    f"{existing.device.by_id} (age {existing.age_s:.1f}s, "
                    f"alive={existing.alive}). This worker holds one console "
                    "session at a time: it is not queued and the port is not "
                    "stolen. Close it first."
                )

            # Resolve before touching the port: a serial mismatch must not
            # cost the board a DTR pulse.
            resolved = resolve_device(device, expect_serial=expect_serial)

            # Under rtscts the driver drives RTS and pyserial does not apply
            # ours; `None` is then the truthful answer to "what is RTS doing".
            effective_rts = None if flow == "rtscts" else rts
            port = self._open_port(resolved, baud=baud, flow=flow, dtr=dtr,
                                   rts=effective_rts)

            try:
                # Constructing the session allocates the capture buffer -- 64
                # MiB by default -- so this is inside the guard, not outside
                # it. `serial.Serial` does inherit `io.RawIOBase.__del__`, so
                # a stranded port would eventually be closed by refcounting,
                # but "eventually" is doing far too much work: a live
                # traceback pins the frame that holds it, a reference cycle
                # defers it to the collector, and until then the fd holds
                # flock(LOCK_EX) on the tty while `_current` is still None --
                # the manager reporting an idle bench whose port nothing can
                # open. Release it here rather than leaving the one exclusive
                # resource on this worker to the garbage collector.
                session = ConsoleSession(
                    session_id="sess-" + uuid.uuid4().hex[:12],
                    device=resolved, port=port, baud=baud, flow=flow,
                    dtr=dtr, rts=effective_rts, capacity=self._capacity,
                )
                # Invariant 2: capture is running before `open` returns, so the
                # boot log is already accumulating when the caller first asks.
                session._reader.start()
            except BaseException:
                port.close()
                raise
            self._current = session
            return session

    def _open_port(self, resolved: ResolvedDevice, *, baud: int, flow: str,
                   dtr: bool, rts: bool | None) -> serial.Serial:
        """Configure everything, then open, so nothing is left at a default.

        pyserial applies `_dtr_state`/`_rts_state` with TIOCMBIS/TIOCMBIC
        inside `open()`; both default to True, which asserts the very lines
        invariant 3 exists to control. They are set here before `open()` so the
        window in which they sit at the driver default is the single `open(2)`
        call itself.
        """
        port = serial.Serial()
        port.port = resolved.tty
        port.baudrate = baud
        port.bytesize = serial.EIGHTBITS
        port.parity = serial.PARITY_NONE
        port.stopbits = serial.STOPBITS_ONE
        port.timeout = READ_TIMEOUT
        port.write_timeout = 5.0
        port.rtscts = (flow == "rtscts")
        port.xonxoff = (flow == "xonxoff")
        # dsrdtr stays False so that pyserial's open() always applies our DTR:
        # it skips `_update_dtr_state()` when dsrdtr is on.
        port.dsrdtr = False
        port.dtr = dtr
        if rts is not None:
            port.rts = rts
        # One owner. Without this a second process -- a stray `screen`, a
        # forgotten miniterm -- silently steals half the byte stream and the
        # capture has holes nobody can see.
        port.exclusive = True
        try:
            port.open()
        except (serial.SerialException, OSError) as exc:
            raise SessionError(
                f"could not open {resolved.by_id} (tty {resolved.tty}) at "
                f"{baud} baud: {exc}"
            ) from exc
        return port

    def get(self, session_id: str) -> ConsoleSession:
        """The live session by id. A reconnecting daemon adopts it this way."""
        with self._lock:
            current = self._current
            if current is None or current.id != session_id:
                raise KeyError(
                    f"no such session: {session_id} "
                    f"(open session: {current.id if current else 'none'})"
                )
            return current

    def close(self, session_id: str) -> None:
        """End the session: stop the reader, release the port, drop the capture.

        The only thing that ends a session. Everything happens under the
        manager lock so a concurrent `open` cannot claim the slot while the
        port is still being released.
        """
        with self._lock:
            current = self._current
            if current is None or current.id != session_id:
                raise KeyError(
                    f"no such session: {session_id} "
                    f"(open session: {current.id if current else 'none'})"
                )
            self.last_close_warning = current._shutdown()
            self._current = None

    def status(self) -> dict:
        """Session state. Valid with nothing open -- that is an answer, not an error.

        Every key in `STATUS_KEYS` is present either way, so a caller reading
        `status()["device"]` gets `None` rather than a `KeyError` when the bench
        is idle.
        """
        with self._lock:
            current = self._current
            if current is None:
                return {key: None for key in self.STATUS_KEYS} | {"open": False}
            return current.describe()
