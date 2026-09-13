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

from .baud import MAX_BAUD_RATE
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

# How long `close` waits for `_write_lock` before it stops waiting *in the
# caller's thread*. It never stops waiting altogether: on expiry the close is
# handed to a daemon thread that waits without a bound, because the fd must not
# be released while a write is inside it. A leaked fd costs one port until the
# worker restarts -- visible, and recoverable by restarting. Delivering one
# session's bytes to the next session's board is neither. With `_closing` set
# before the acquire, queued writers refuse instantly instead of each starting a
# fresh `write_timeout`, so only one in-flight write can ever be outstanding and
# this bound should not be reachable.
WRITE_DRAIN_TIMEOUT = 6.0

# How long `scan_baud` waits for the reader thread to park (stop calling
# `port.read()`) before giving up on the scan. A reader loop iteration is
# bounded by READ_TIMEOUT, so this is generous slack, not a tight fit --
# exceeding it means the reader is wedged somewhere it should not be, and the
# scan refuses rather than risk two readers on the same fd.
SCAN_PARK_TIMEOUT = 1.0

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
        # Set before `_shutdown` queues for `_write_lock`, so a writer waiting
        # on that lock refuses the moment it gets in rather than starting a
        # fresh `write_timeout`. Without it, N queued writers extend the close
        # by N * write_timeout and the drain bound means nothing.
        self._closing = threading.Event()
        self._stop = threading.Event()
        # `scan_baud` sets `_scan_pause` to ask the reader to stop calling
        # `port.read()` -- two readers on one fd would race for bytes -- and
        # waits for `_scan_parked` before it touches the port itself, so the
        # scan is provably the only thing reading while it changes the line
        # speed underneath what would otherwise be a live capture.
        self._scan_pause = threading.Event()
        self._scan_parked = threading.Event()
        # A gap ledger, out-of-band from the byte-exact capture (tools.py
        # requires the capture stay byte-exact evidence, so a gap can never
        # be an in-band marker). `CaptureBuffer.read` already reports
        # `dropped` for bytes evicted by wraparound; it has no way to report
        # bytes that were never captured at all, because the cursor space it
        # works in has nothing to record there -- a suspended capture leaves
        # no hole in the byte stream, only a hole in time. Recorded here
        # instead: `{at_cursor, duration_s, reason}` per suspension, a leaf
        # lock of its own so a reader (`console_status`, `console_read`)
        # never has to take `_write_lock` or `_state_lock` to see it.
        self._gaps: list[dict] = []
        self._gaps_lock = threading.Lock()
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

    def _record_gap(self, *, at_cursor: int, duration_s: float, reason: str,
                    in_progress: bool = False) -> dict:
        """Log a capture suspension AS IT STARTS, not after it ends.

        Called by the SCAN thread as soon as it observes the reader has
        parked (`in_progress=True`, `duration_s` a placeholder), not in a
        `finally` once the whole scan is over: a suspension that has been
        running for several seconds when a concurrent `console_read`/
        `console_status` call lands is exactly the reasoning hazard the gap
        ledger exists to prevent, and it does not wait for the scan to
        finish before it applies. Not quite "the instant the reader parks":
        the reader (a different thread) sets `_scan_parked` and this is
        called after the scan's own `.wait()` on it returns, so there is a
        small gap between the two -- measured ~0.6ms, wider under cross-core
        scheduling contention -- during which a landing call still sees the
        pre-scan state. Returns the entry object itself (not a copy) so
        `_finish_gap` can update it in place once the real duration is
        known.
        """
        entry = {"at_cursor": at_cursor, "duration_s": round(duration_s, 3),
                 "reason": reason, "in_progress": in_progress}
        with self._gaps_lock:
            self._gaps.append(entry)
        return entry

    def _finish_gap(self, entry: dict, duration_s: float) -> None:
        """Fill in the real duration once the suspension that `entry`
        recorded has actually ended."""
        with self._gaps_lock:
            entry["duration_s"] = round(duration_s, 3)
            entry["in_progress"] = False

    def gaps_overlapping(self, start_cursor: int, end_cursor: int) -> list[dict]:
        """Every gap whose `at_cursor` sits inside `[start_cursor, end_cursor]`.

        A suspension leaves no byte range in cursor space -- capture just
        stops advancing `head` for a while and resumes from the same offset,
        so there is nothing for `dropped` to count. What CAN be checked is
        whether a `read()` call's returned window straddles the point where
        that happened: if so, the bytes on either side of `at_cursor` look
        perfectly contiguous to the caller, and it is exactly that caller --
        the one reasoning over what it just read -- who needs to be told.
        """
        with self._gaps_lock:
            return [dict(g) for g in self._gaps
                    if start_cursor <= g["at_cursor"] <= end_cursor]

    def gap_summary(self) -> dict:
        """Count and total seconds of every capture suspension, PLUS whether
        one is happening right now.

        `total_seconds` sums each entry's `duration_s` -- which is a
        placeholder (0.0) for whichever entry is still `in_progress`, so a
        suspension in its 12th second reads as "0.0s" here. That pairing --
        count: 1, seconds: 0.0 -- looks like "a gap of zero length", which is
        as misleading as reporting no gap at all: it is the exact reasoning
        hazard this ledger exists to prevent, just relocated from "invisible"
        to "invisible AND presented as a number". `capture_suspended` is the
        explicit signal a caller needs instead of inferring "in progress"
        from a seconds value that cannot show it.
        """
        with self._gaps_lock:
            return {
                "count": len(self._gaps),
                "total_seconds": round(sum(g["duration_s"] for g in self._gaps), 3),
                "capture_suspended": any(g["in_progress"] for g in self._gaps),
            }

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
            if self._scan_pause.is_set():
                # A baud scan owns the port: step aside so it is the only
                # thing calling port.read() while it changes line speed, and
                # touch neither the port nor the buffer until it hands the
                # port back. Appending here would risk two readers racing for
                # bytes on one fd, and -- the harm that actually matters --
                # could land a sample taken at a candidate rate the target
                # was never using into the capture, as if the target had said
                # it at the session's real rate.
                self._scan_parked.set()
                while self._scan_pause.is_set() and not self._stop.is_set():
                    time.sleep(0.01)
                self._scan_parked.clear()
                continue
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
            if self._closed or self._closing.is_set():
                raise SessionError(
                    f"session {self.id} is closing or closed; nothing was "
                    "written"
                )
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
                written = self._serial.write(data)
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

            # pyserial's `cancel_write()` abort path is
            # `os.read(pipe_abort_write_r, 1000); break` followed by
            # `return length - len(d)` (serialposix.py:632-634, 662) -- it
            # raises nothing and reports the short count in its return value.
            # Discarding that value would let a `console_send` racing a
            # `console_close` return success having put zero bytes on the wire,
            # telling a language model its command reached the board when
            # nothing did. On a hardware console a false report about physical
            # state is the worst thing this worker can do.
            #
            # The count is a LOWER BOUND, not a boundary. pyserial's abort
            # `break` (serialposix.py:633) happens before `d = d[n:]`
            # (:658), so the final `os.write` of the loop is never deducted
            # from `length - len(d)` -- and every iteration writes before it
            # selects, so there is always exactly one such chunk. Measured on
            # a slow-draining pty: 64000 reported, 73728 actually delivered.
            # Stating it as exact would invite a caller to resume from that
            # offset and resend bytes that already reached the board, on a
            # live console driving hardware.
            if written != len(data):
                raise SessionError(
                    f"write on session {self.id} was aborted after at least "
                    f"{written} of {len(data)} bytes (the session is being "
                    "closed). The exact boundary is not knowable -- pyserial's "
                    "abort drops the last chunk from its count -- so more than "
                    f"{written} bytes may have reached the target. Assume the "
                    "command did not take effect, and do not resume from this "
                    "offset."
                )

    def scan_baud(self, rates: tuple[int, ...], dwell_seconds: float,
                  decide, budget_seconds: float | None = None,
                  early_exit=None) -> dict:
        """SWEEP the line across `rates`, on THIS descriptor, without
        transmitting -- then leave the port at whatever `decide` picks.

        Not one pass. The candidate list is swept REPEATEDLY, dwelling
        `dwell_seconds` on each rate per pass, until `budget_seconds` of
        sampling has elapsed or `early_exit` accepts a candidate. A
        candidate's bytes accumulate across its windows; `decide` still sees
        one `bytes` per rate.

        Why, in one measured sentence: a target that emits a burst at boot
        and then goes silent (verified -- 12 s of listening after boot
        returned 0 bytes) gives a single-pass scan exactly one candidate's
        worth of evidence, and that candidate is whichever one happened to
        hold the window when the burst arrived. It then "wins" regardless of
        whether it is right, and every other rate reports silence. Sweeping
        puts a multi-second burst into several windows PER RATE, so only the
        correct rate accumulates readable text. `baud.DEFAULT_DWELL_SECONDS`
        carries the rest of the reasoning.

        `budget_seconds` defaults to exactly one full sweep
        (`len(rates) * dwell_seconds`), which is the old single-pass
        behaviour and is what the session-level tests exercise when they do
        not ask for more.

        `early_exit(data: bytes) -> bool`, when given, is offered every
        candidate's accumulated bytes AFTER EACH COMPLETED SWEEP, never
        mid-sweep. That ordering is the honesty guarantee: by the time an
        early exit can be taken, every candidate the hardware accepted has
        been sampled and appears in the result with evidence of its own. It
        does mean the earliest possible exit is the end of sweep 1 rather
        than the instant a good sample lands -- the cost of one sweep, paid
        so the ranking can never be silent about a rate that was skipped.
        How long each rate was actually listened to comes back in
        `"listen_seconds"`, because an early exit leaves those unequal.

        Never closes and reopens: reopening re-asserts DTR, which would
        reboot a DTR-reset board once per dwell -- and a sweep has many more
        dwells than a single pass has candidates, so this matters more now,
        not less. Instead this changes `baudrate` via termios on the
        already-open fd between candidates. Verified on the bench across a
        real scan: 0 DTR transitions and 0 reopens over 178 samples.

        `decide(samples: dict[rate, bytes]) -> int | None` is called exactly
        once, with every candidate's raw bytes, after the last one is
        sampled and before the port is touched again -- so the decision is
        applied atomically with the rest of the scan, and the reader thread
        (paused for the whole call, see `_read_forever`) never observes an
        intermediate rate. Returning `None` restores the rate the session was
        on when the scan started; a returned rate is left live.

        Holds `_write_lock` for the ENTIRE scan, not just the port
        reconfiguration: `write()` takes the same lock, so a concurrent
        `console_send` blocks rather than putting bytes on the wire while the
        rate is in flux -- a wrong-rate write is framing garbage arriving at
        whatever the target's bootloader is, exactly the hazard "detection
        never transmits" exists to avoid. A `close()` racing this still
        completes: `_stop`/`_closing` are checked between candidates and
        inside each candidate's read loop, and `_shutdown`'s `cancel_read()`
        interrupts whichever read is in flight, so the scan aborts and
        releases `_write_lock` well inside `_shutdown`'s own drain timeout.

        Every candidate value must be a positive integer no larger than
        `MAX_BAUD_RATE` -- defence in depth alongside `baud.sanitize_rates`,
        since pyserial's custom-baud path turns a kernel-rejected rate into
        `ValueError` and the termios `array('i')` store overflows for
        anything at or above 2**31, neither of which is safe to leave
        unhandled from a tier-low, never-prompted tool. A candidate that
        PASSES that check but the hardware itself still refuses at apply
        time does NOT abort the scan: it is recorded in the returned
        `"rejected"` dict (`{rate: reason}`) and the next candidate is tried.
        Invariant 5 is ranked evidence, never a bare verdict -- discarding
        every other candidate's perfectly good sample because one rate the
        specific chip does not support raised would be exactly that bare
        verdict, just spelled as an exception instead of a guess.

        However this returns or raises, the port is left at a KNOWN rate
        (the winner, or the rate the session started at) and `self.baud`
        matches it -- a bad-but-in-range candidate that pyserial itself
        rejects must never leave the line at whatever tcsetattr last
        applied while `self.baud` still claims something else.

        Pausing the reader for the scan means capture stops advancing for
        the whole scan budget or more -- a real gap in the boot log a caller
        could otherwise reason straight across. ONE gap entry covers the
        whole sweep, not one per dwell: the reader parks once, before the
        first candidate, and resumes once, after the last.
        Unlike `"rejected"`, that gap is not only reported after the fact:
        `_record_gap` is called as soon as THIS (scan) thread observes the
        park -- i.e. right after `_scan_parked.wait()` returns, not "the
        instant the reader parks" (a different thread sets that event; the
        two are not the same moment, only close -- measured ~0.6ms apart
        naturally, wider under cross-core scheduling contention). A
        concurrent `console_read` sees the entry (via `gaps_overlapping`,
        `"in_progress": True`, a placeholder duration) WHILE the scan is
        happening, not only on the next call once this one has returned --
        modulo that same small bound, during which a landing call still sees
        the pre-scan state. A concurrent `console_status` sees
        `capture_suspended: True` the same way, subject to the same bound --
        but NOT a `duration_s` that has grown to match: `gap_summary`'s
        `total_seconds` sums each entry's `duration_s`, which is still the
        0.0 placeholder for the in-progress one, so `capture_suspended` is
        the explicit signal, not the seconds. `_finish_gap` fills in the
        real duration when the scan ends, whatever the reason. Returned here
        as `"gap"`.

        A candidate's read loop makes the same by-id check `_read_forever`
        makes on a quiet line: `udev` removes the symlink on unplug, so its
        absence is positive evidence the adapter left the bus rather than an
        inference from silence. Without it an unplugged adapter produces a
        run of empty samples that score exactly like a line whose ground is
        floating, and tools.py's wiring hint would be attached to a device
        that is simply gone. `death_reason` is set, the candidate's sample is
        still recorded, and the scan stops on the existing `if not
        self.alive` check below it.

        Raises `SessionError` for a bad rate list, a session that is not
        alive, a reader that will not park, or a `decide` callback that
        raises -- always with the port left at a known rate. Does NOT raise
        for a candidate rate the hardware rejects, nor for a device that dies
        mid-scan; see `"rejected"` and `"death_reason"` above.
        """
        if not rates:
            raise SessionError("scan_baud: no candidate rates given")
        if any((not isinstance(r, int)) or isinstance(r, bool)
               or not (0 < r <= MAX_BAUD_RATE) for r in rates):
            raise SessionError(
                f"scan_baud: refusing rate list {rates!r} -- every rate must "
                "be a positive integer no greater than "
                f"{MAX_BAUD_RATE} (0 deasserts the modem control lines; "
                "pyserial's custom-baud path can raise for anything larger)"
            )

        acquired = self._write_lock.acquire(timeout=WRITE_DRAIN_TIMEOUT)
        if not acquired:
            raise SessionError(
                f"session {self.id}: could not get exclusive access to the "
                f"port within {WRITE_DRAIN_TIMEOUT}s (a write is in flight); "
                "refusing to scan rather than contend with it"
            )
        try:
            if self._closed or self._closing.is_set():
                raise SessionError(
                    f"session {self.id} is closing or closed; refusing to scan"
                )
            if not self.alive:
                raise SessionError(
                    f"session {self.id} is not alive, refusing to scan: "
                    f"{self.death_reason}"
                )

            original_baud = self.baud
            if budget_seconds is None:
                # Exactly one full sweep: the single-pass shape, as the
                # degenerate case of the sweep rather than a separate code
                # path that could drift away from it.
                budget_seconds = len(rates) * dwell_seconds
            samples: dict = {}
            listened: dict[int, float] = {}
            rejected: dict[int, str] = {}
            sweeps = 0
            early_exited = False
            winner: int | None = None
            # Pre-set to the fallback: if anything below raises before a
            # winner is chosen, the `finally` still restores a KNOWN rate
            # rather than leaving the port at whatever the failed candidate
            # left in the termios struct.
            final_baud = original_baud
            gap_start_time: float | None = None
            gap_entry: dict | None = None

            self._scan_pause.set()
            try:
                parked = self._scan_parked.wait(timeout=SCAN_PARK_TIMEOUT)
                if not parked:
                    raise SessionError(
                        f"session {self.id}: reader thread did not park "
                        f"within {SCAN_PARK_TIMEOUT}s; refusing to scan "
                        "rather than risk two readers on one port"
                    )

                # The gap begins here, not at `_scan_pause.set()`: this is
                # the instant the reader has actually stopped reading, which
                # is also the instant `head` stops advancing. Recorded (not
                # just timestamped) immediately, and visible to a concurrent
                # console_status/console_read for the whole scan -- not only
                # after the fact -- because a suspension in progress is
                # exactly the same reasoning hazard for a caller mid-read as
                # one already finished; the only thing it does not yet know
                # is how long it will run.
                buffer = self._buffer
                gap_start_time = time.monotonic()
                gap_entry = self._record_gap(
                    at_cursor=buffer.head if buffer is not None else 0,
                    duration_s=0.0,
                    reason="baud scan",
                    in_progress=True,
                )

                try:
                    # -- THE SWEEP ------------------------------------
                    # Repeated short dwells over the WHOLE candidate list,
                    # not one long dwell per candidate. See
                    # `baud.DEFAULT_DWELL_SECONDS` for the target that
                    # forced this: a board that emits a burst at boot and
                    # then goes silent gives a single-pass scan exactly one
                    # window's worth of evidence, and whichever candidate
                    # happened to hold that window "wins" by luck.
                    #
                    # Nothing about the reader park or the gap ledger moves
                    # for this. The port is opened once and never reopened
                    # (a reopen re-asserts DTR and would reboot a
                    # DTR-reset board once per dwell -- dozens of times
                    # across a sweep rather than once per candidate), the
                    # reader stays parked for the whole scan, and ONE gap
                    # entry spans it, recorded before this loop and
                    # finished after it. Sweeping changes only which rate
                    # this thread has applied at any instant.
                    scan_deadline = time.monotonic() + budget_seconds
                    pending = list(rates)
                    while pending:
                        swept = True
                        for rate in pending:
                            if self._stop.is_set() or self._closing.is_set():
                                swept = False
                                break
                            # The FIRST sweep is never cut short by the
                            # budget: it is what makes every candidate
                            # appear in the result with evidence of its
                            # own, so an early exit later cannot leave the
                            # ranking silent about a rate it never tried.
                            # `check_budget` refuses a list too long to
                            # sweep once, so this cannot overrun by more
                            # than one sweep even when a caller passes its
                            # own budget.
                            if sweeps >= 1 and time.monotonic() >= scan_deadline:
                                swept = False
                                break
                            try:
                                self._serial.baudrate = rate
                                # Discard whatever is sitting in the
                                # driver's input queue from the PREVIOUS
                                # candidate rate (or from before the scan
                                # started): those bytes were framed at a
                                # different speed and are not evidence
                                # about `rate`. This does discard a few
                                # genuine at-the-original-rate bytes the
                                # driver had queued but the reader had not
                                # yet drained -- an unavoidable part of the
                                # same gap, which is exactly why the gap is
                                # measured from `gap_start_time` (before
                                # this call), not from the first
                                # candidate's read loop.
                                self._serial.reset_input_buffer()
                            except (serial.SerialException, OSError) as exc:
                                self._die(self._explain(exc, "baud scan (set rate)"))
                                raise SessionError(
                                    f"session {self.id}: setting {rate} baud "
                                    f"failed and the device looks gone: "
                                    f"{self.death_reason}"
                                ) from exc
                            except (ValueError, OverflowError) as exc:
                                # pyserial's custom-baud path converts a
                                # kernel-rejected rate to ValueError
                                # (serialposix.py's _set_special_baudrate),
                                # and the termios array('i') store raises
                                # OverflowError for a rate that does not fit
                                # a C int (>= 2**31). Neither means the
                                # device is gone, and neither means the REST
                                # of the scan is untrustworthy: invariant 5
                                # is ranked evidence, never a bare verdict,
                                # and aborting every other candidate over one
                                # the hardware itself refuses would throw
                                # away perfectly good samples already in hand
                                # (or still to come) for no reason connected
                                # to them. Record the rejection and try the
                                # next rate; it is dropped from `pending`
                                # below so the sweep does not spend a termios
                                # call re-learning the same refusal every
                                # pass.
                                rejected[rate] = str(exc)
                                continue

                            collected = samples.setdefault(rate, bytearray())
                            dwell_start = time.monotonic()
                            deadline = dwell_start + dwell_seconds
                            while (time.monotonic() < deadline
                                   and not self._stop.is_set()
                                   and not self._closing.is_set()):
                                try:
                                    chunk = self._serial.read(READ_CHUNK)
                                except (serial.SerialException, OSError) as exc:
                                    self._die(self._explain(exc, "baud scan (read)"))
                                    break
                                if chunk:
                                    collected.extend(chunk)
                                    continue
                                # A quiet line is the same two-way ambiguity
                                # `_read_forever` resolves, and it matters
                                # MORE here: an empty sample is scored as
                                # evidence about `rate`, and a scan whose
                                # every candidate came back empty because the
                                # adapter left the bus is indistinguishable,
                                # by score alone, from one whose ground is
                                # floating. tools.py hands the second case a
                                # wiring hint. Without this check the first
                                # case gets it too -- an operator told to
                                # inspect the ground on a device that is
                                # simply gone.
                                if not os.path.lexists(self.device.by_id):
                                    self._die(
                                        f"device {self.device.by_id} disappeared "
                                        f"during a baud scan at {rate}: the by-id "
                                        f"path no longer resolves (was "
                                        f"{self.device.tty}) and the line has gone "
                                        "quiet"
                                    )
                                    break
                            listened[rate] = (listened.get(rate, 0.0)
                                              + time.monotonic() - dwell_start)
                            if not self.alive:
                                swept = False
                                break

                        if rejected:
                            pending = [r for r in pending if r not in rejected]
                        if not swept:
                            break
                        sweeps += 1
                        # Decided on a COMPLETED sweep only, so an early
                        # exit is never taken on the strength of evidence
                        # some candidate has not had the chance to produce.
                        if early_exit is not None and any(
                                early_exit(bytes(data))
                                for data in samples.values() if data):
                            early_exited = True
                            break
                        if time.monotonic() >= scan_deadline:
                            break

                    samples = {rate: bytes(data) for rate, data in samples.items()}

                    try:
                        winner = decide(samples) if samples else None
                    except Exception as exc:  # noqa: BLE001 -- a 3rd-party
                        # decide callback must not escape as anything other
                        # than SessionError, and must not skip the restore
                        # below (it runs in the enclosing `finally`).
                        raise SessionError(
                            f"session {self.id}: the baud-scan decision "
                            f"callback raised: {type(exc).__name__}: {exc}"
                        ) from exc
                    final_baud = winner if winner is not None else original_baud
                finally:
                    # Runs on every path out of the block above -- success,
                    # break, or an exception -- so the port is NEVER left at
                    # an undefined rate with `self.baud` claiming something
                    # else. `final_baud` defaults to `original_baud` (set
                    # before the try), so a bad candidate mid-loop restores
                    # the rate the session started at.
                    try:
                        self._serial.baudrate = final_baud
                        self._serial.reset_input_buffer()
                    except Exception:  # noqa: BLE001 -- best effort; the
                        pass          # device may already be gone
                    self.baud = final_baud
            finally:
                self._scan_pause.clear()
                # Guarded on `gap_entry`, NOT `gap_start_time`: the latter is
                # set one line before `_record_gap` is called, so anything
                # raising in that narrow window (essentially unreachable --
                # `_record_gap` only does a dict literal and a lock-guarded
                # append -- but not provably impossible) would otherwise call
                # `_finish_gap(None, ...)` and mask the real exception behind
                # a bare TypeError.
                if gap_entry is not None:
                    self._finish_gap(gap_entry, time.monotonic() - gap_start_time)
        finally:
            self._write_lock.release()

        return {
            "samples": samples,
            "rejected": rejected,
            "listen_seconds": listened,
            "sweeps": sweeps,
            "early_exit": early_exited,
            "budget_seconds": budget_seconds,
            "original_baud": original_baud,
            "final_baud": final_baud,
            # `winner is None`, NOT `final_baud == original_baud`: a `decide`
            # that correctly picks the rate the session was already on is a
            # real winner, not a fallback restore, even though the two are
            # indistinguishable by rate alone.
            "winner": winner,
            "restored": winner is None,
            "alive": self.alive,
            "death_reason": self.death_reason,
            "gap": gap_entry,
        }

    def _shutdown(self) -> str | None:
        """Stop the reader, release the port, discard the capture.

        Only `SessionManager.close` calls this. Returns a warning string if the
        reader would not join, so the caller can surface it rather than have it
        vanish.
        """
        warnings: list[str] = []
        # Before anything else, and before queueing for `_write_lock`: a writer
        # already waiting on that lock must refuse when it gets in rather than
        # begin a new `write_timeout` behind us.
        self._closing.set()
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
            if acquired:
                self._release_port()
            else:
                # Do NOT close the fd here. A write is still inside
                # `serial.write()`, which re-reads `self.fd` every iteration,
                # and closing now is precisely the harm the lock exists to
                # prevent -- the next `open` can be handed the same fd number.
                # Hand the close to a daemon that waits without a bound: the
                # port stays held until the write really finishes, and the next
                # `open` on this device fails loudly on the exclusive lock
                # rather than quietly writing to the wrong board.
                threading.Thread(
                    target=self._release_port_when_drained,
                    name=f"console-drain-{self.id}",
                    daemon=True,
                ).start()
                warnings.append(
                    f"a write on session {self.id} was still in flight after "
                    f"{WRITE_DRAIN_TIMEOUT}s, so {self.device.by_id} is still "
                    "held and will be released when that write returns. The "
                    "session is over; re-opening this device may fail until "
                    "then."
                )
            with self._state_lock:
                self._alive = False
            # On the `acquired` path these run under `_write_lock`; on the
            # expiry path they do not, and that is safe for reasons that have
            # changed since this was first written. `_closing` -- set before
            # the acquire above -- is what now stops a queued writer, so
            # `_closed` is no longer the flag a writer races for. And
            # `_read_forever` holds its own reference to the buffer, taken
            # when the thread started, so dropping `_buffer` here cannot trip
            # a reader that is still finishing its last append.
            self._closed = True
            self._buffer = None
        finally:
            if acquired:
                self._write_lock.release()
        return "; ".join(warnings) if warnings else None

    def _release_port(self) -> None:
        """Close the fd. The caller must hold `_write_lock`."""
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001 -- an unplugged fd fails to close; the
            pass           # session is over either way and must not hang here

    def _release_port_when_drained(self) -> None:
        """Wait for the in-flight write, however long it takes, then close."""
        with self._write_lock:
            self._release_port()

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
        gaps = self.gap_summary()
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
            "capture_gap_count": gaps["count"],
            "capture_gap_seconds": gaps["total_seconds"],
            "capture_suspended": gaps["capture_suspended"],
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
        "capture_gap_count", "capture_gap_seconds", "capture_suspended",
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
            hint = ""
            warning = self.last_close_warning
            if warning:
                # Otherwise this surfaces to a language model as a bare EAGAIN
                # immediately after a close it just watched succeed, with
                # nothing to connect the two.
                hint = f" (note: {warning})"
            raise SessionError(
                f"could not open {resolved.by_id} (tty {resolved.tty}) at "
                f"{baud} baud: {exc}{hint}"
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

        The only thing that ends a session.

        The slot is freed under the manager lock; the shutdown itself runs
        outside it. `_shutdown` can wait seconds -- up to `JOIN_TIMEOUT` for
        the reader and `WRITE_DRAIN_TIMEOUT` for an in-flight write -- and
        `status`, `get` and `open` are all tool calls that would otherwise
        queue behind it.

        The cost is that a *concurrent* `open` on the same device, arriving
        between the slot being freed and the port being released, fails on the
        exclusive lock. `last_close_warning` is therefore *published* under the
        manager lock before the shutdown starts, not cleared: an `open` landing
        inside that window has to be able to explain its own failure, and that
        window is the whole reason the warning exists. It is replaced with the
        shutdown's real outcome -- `None`, or the drain warning -- once
        `_shutdown` returns. A sequential close-then-open is unaffected either
        way, because `_shutdown` has returned before `close` does.
        """
        with self._lock:
            current = self._current
            if current is None or current.id != session_id:
                raise KeyError(
                    f"no such session: {session_id} "
                    f"(open session: {current.id if current else 'none'})"
                )
            self._current = None
            # Provisional, and deliberately not None: for as long as
            # `_shutdown` runs, this is the only thing that can tell a
            # concurrent `open` why the port it just saw freed will not open.
            self.last_close_warning = (
                f"session {current.id} on {current.device.by_id} is closing; "
                "the port may be held for a moment longer"
            )
        self.last_close_warning = current._shutdown()

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
