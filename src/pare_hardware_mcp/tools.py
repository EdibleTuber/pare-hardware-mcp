"""Tool handlers. Names match contract.py exactly; server.py binds by name.

TARGET BYTES ARE UNTRUSTED AND TRAVEL BASE64. UART output is arbitrary bytes,
not text, and it reaches the model's context, the capture store, /snapshot and
-- via the next call's argument snapshot -- the operator's approval prompt. The
artifact design applies a control-character check to `path` and `drive_id` for
exactly this reason; this is the same problem with far more volume. Encode here,
strip at render time, and keep the capture byte-exact because it is evidence.

NOTHING BLOCKING RUNS ON THE EVENT LOOP, AND NO HANDLER MAY ASSUME IT IS ALONE.
The two are one rule, not two. `ConsoleSession.write` waits on `_write_lock`,
which `scan_baud` holds for a whole scan and `_shutdown` holds for up to
WRITE_DRAIN_TIMEOUT + JOIN_TIMEOUT; `SessionManager.close` waits on both.
Called straight from an `async def`, either one stops every other tool call on
this worker -- measured end-to-end over a pty: `console_send` blocked 3.78 s
and a 50 ms status poller completed 0 of the ~75 polls due in that window.
They run via `asyncio.to_thread`, and so does EVERY `MANAGER` call, as a flat
rule rather than a per-call-site judgement. `SessionManager.open` holds the
manager `_lock` across `resolve_device`, the `open(2)` on the tty, the 64 MiB
buffer allocation and the reader thread's start (session.py:875-920) -- so
once `console_open` itself is on a thread, any handler that touches `MANAGER`
on the event loop can be parked on that lock for the whole of an open. `get`,
`current` and `status` are each microseconds on their own; what makes them
unsafe inline is who else holds the lock they take, and that is not a property
any one call site can check. The cost is one thread hop per tool call against
a call that arrived over a network.

But the event loop was also the only thing keeping the handlers safe from each
other. `console_read` reads `MANAGER.get(...)` and then `sess.buffer` with no
await in between, and `ConsoleSession.buffer` raises `SessionError` once a
close has discarded the capture. Moving the blocking calls to threads makes
that interleaving reachable, so every handler catches what a CONCURRENT close
can now make its session raise -- otherwise the {"error": ...} contract is
escaped as an opaque transport failure exactly when the model most needs to
read what happened. Both halves belong in the same change; neither is safe
alone.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any

from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_SAMPLE_SECONDS,
                                    GROUND_CROSSOVER_HINT, all_scored_poorly,
                                    all_silent, check_budget, pick_winner,
                                    rank_candidates, sanitize_rates)
from pare_hardware_mcp.config import load_config
from pare_hardware_mcp.devices import list_serial_devices
from pare_hardware_mcp.ringbuffer import (CursorError, DEFAULT_READ_LIMIT,
                                          MAX_READ_LIMIT)
from pare_hardware_mcp.session import SessionError, SessionManager

CONFIG = load_config()
MANAGER = SessionManager(capacity=CONFIG.buffer_bytes)

# A `.bench-store-id` value reaches an operator's terminal via bench_status --
# the same "escape sequence rewrites what the operator sees" problem the
# artifact-wiring design's `_CONTROL_RE` addresses for `path` and `drive_id`.
# Stripped here, at the point this worker reads it off disk, rather than
# trusted through to the caller.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _manager_current():
    """`MANAGER.current` as a callable, so it can be handed to a thread.

    Reads the module global at call time, which is what lets tests replace
    `tools.MANAGER` and still exercise this path.
    """
    return MANAGER.current


def _ok(**fields: Any) -> str:
    return json.dumps(fields)


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _read_limit(limit: int | None) -> int:
    """`limit` as a BOUNDED byte count. Never `None`, never unbounded.

    `CaptureBuffer.read` keeps `limit=None` meaning "everything"; that is
    correct for an in-process caller and stays as it is. This is the tool
    boundary, where the result is base64 in JSON bound for a language model's
    context: an unbounded default read of a full 64 MiB buffer measured 89.5
    MB and 514 ms. `remaining`/`next_cursor` already let a caller drain across
    several calls, so a ceiling costs nothing that was not already supported.

    A non-integer `limit` is refused rather than clamped, because
    `min("4096", n)` raises `TypeError` and no caller-facing except clause in
    `console_read` catches it -- it would escape the `{"error": ...}` contract
    as an opaque transport failure.

    That is defence in depth, not a reachable wire bug, and the difference was
    checked rather than assumed: driving `console_read` through `build_server()
    .call_tool` shows FastMCP's pydantic model validating `limit` first. It
    COERCES `"4096"`, `4096.0` and even `True` to an int, and rejects `4096.5`
    with its own ToolError before this function is reached, so over the wire
    `limit` is always a real int or never arrives. The guard exists for
    in-process callers (this module's own tests among them) and for any future
    dispatch path that does not validate, which is exactly the kind of
    assumption that should not be load-bearing in a handler.
    """
    if limit is None:
        return DEFAULT_READ_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be an integer number of bytes, got {limit!r}"
        )
    return max(0, min(limit, MAX_READ_LIMIT))


async def console_read(session: str, cursor: int = 0,
                       limit: int | None = None) -> str:
    try:
        capped = _read_limit(limit)
    except ValueError as exc:
        return _err(str(exc))
    try:
        sess = await asyncio.to_thread(MANAGER.get, session)
    except KeyError:
        return _err(f"no such session {session!r}")
    try:
        # `sess.buffer` and `.read()` stay inline: the buffer's lock is a leaf
        # held only for a memory copy of at most MAX_READ_LIMIT bytes by the
        # one reader thread, which is microseconds (measured: 0.54 ms for the
        # whole handler at the default limit, 1.40 ms at the ceiling --
        # base64 included).
        data, next_cursor, dropped, remaining = sess.buffer.read(cursor, capped)
    except CursorError:
        # `cursor` is negative or ahead of `head`: not a position this API
        # ever handed out, so it can never be a legitimate "fell behind"
        # case. Let it escape and it becomes an opaque transport-level
        # failure instead of something the model can read and correct.
        return _err(f"invalid cursor {cursor} for session {session!r}")
    except SessionError as exc:
        # A close landed between `MANAGER.get` returning this session and
        # `sess.buffer` being read: `_shutdown` sets `_buffer = None`, and
        # the property raises rather than hand back a capture belonging to a
        # session that is over (session.py:186-193). There is no await
        # between those two lines, so before console_close moved off the
        # event loop this was unreachable -- and that is exactly why it has
        # to be caught in the same change that makes it reachable.
        return _err(str(exc))
    # `dropped` only ever counts bytes evicted by ring-buffer wraparound --
    # it cannot see a capture that was SUSPENDED (a baud scan pausing the
    # reader), because that leaves no byte range in cursor space at all,
    # only a hole in time. `capture_gaps` is the other half: any suspension
    # whose recorded cursor position falls inside the window just returned,
    # so this byte-contiguous stream does not read as an unbroken one to
    # whatever -- a language model, most likely -- consumes it next.
    capture_gaps = sess.gaps_overlapping(next_cursor - len(data), next_cursor)
    # `limit_applied` is how a caller learns the ceiling from a RESPONSE and
    # not only from the tool description: it asked for 10 MB, it got
    # MAX_READ_LIMIT, and `remaining` says the rest is still there. Without
    # it, a clamp is indistinguishable from a buffer that happened to hold
    # exactly that much.
    return _ok(session=session,
               data_b64=base64.b64encode(data).decode("ascii"),
               next_cursor=next_cursor, dropped=dropped, remaining=remaining,
               limit_applied=capped,
               alive=sess.alive, capture_gaps=capture_gaps)


async def console_status() -> str:
    """Session state. Off the loop for the manager lock -- see the module note."""
    return _ok(**await asyncio.to_thread(MANAGER.status))


async def console_close(session: str) -> str:
    """Release the port and end the session.

    Off the event loop: `SessionManager.close` runs `_shutdown`, which joins
    the reader (up to JOIN_TIMEOUT = 2.0s) and then waits on `_write_lock`
    (up to WRITE_DRAIN_TIMEOUT = 6.0s) because the fd must not be closed
    under an in-flight write. Run inline, that is up to 8 seconds in which
    console_status and console_read cannot answer.

    `KeyError` stays the only thing caught, and that is a claim about
    `_shutdown`'s surface rather than an oversight: every fallible call in it
    is individually wrapped -- `cancel_read`/`cancel_write` at
    session.py:714-718, `_serial.close()` at :782-785 -- and the joins and
    timed acquires do not raise. A second close of the same session takes
    the `KeyError` path, because the slot is cleared under the manager lock
    before the shutdown begins.
    """
    try:
        await asyncio.to_thread(MANAGER.close, session)
    except KeyError:
        return _err(f"no such session {session!r}")
    return _ok(closed=session)


async def console_open(device: str | None = None, baud: int = 115200,
                       flow: str = "none", dtr: bool = False,
                       rts: bool = False) -> str:
    """Open the console and start capturing.

    `expect_serial` is deliberately NOT a parameter here: the caller of this
    tool is a language model, and a caller-supplied expected serial would
    defeat the check entirely. The operator declares it out of band, via
    `PARE_HW_EXPECT_SERIAL` in `CONFIG`; this handler always passes that
    value, never one from the wire. Similarly, `device` falls back to
    `PARE_HW_DEVICE` when the caller omits it, since a networked worker has
    no other channel for an operator default (see config.py).
    """
    device = device or CONFIG.device
    if not device:
        return _err(
            "no device given and PARE_HW_DEVICE is not set: the operator "
            "must configure a default by-id device path for this worker"
        )
    try:
        # Off the event loop as well: this allocates the 64 MiB capture
        # buffer, opens a tty (an `open(2)` on a wedged USB node is not
        # instant), starts the reader thread, and can queue on the manager
        # lock behind another open. The broad except is unchanged -- it
        # already covered DeviceError, SessionError and anything pyserial
        # raises, and `to_thread` re-raises in this frame, so nothing about
        # which exceptions reach it changes.
        sess = await asyncio.to_thread(
            MANAGER.open, device=device, expect_serial=CONFIG.expect_serial,
            baud=baud, flow=flow, dtr=dtr, rts=rts)
    except Exception as exc:                      # noqa: BLE001 - surface it
        return _err(str(exc))
    # Report the lines we asserted -- and, for rts, what the session actually
    # did rather than what was requested. Under flow="rtscts" pyserial never
    # applies a requested RTS, so `sess.rts` is `None` there; `json.dumps`
    # serialises that as `null`, never coerced to `false` -- this line resets
    # target boards, so a false claim about it is a real defect.
    return _ok(session=sess.id, device=sess.device.by_id,
               serial=sess.device.serial, baud=sess.baud, flow=sess.flow,
               dtr=sess.dtr, rts=sess.rts, cursor=sess.buffer.head,
               note="target voltage is set by a physical switch on the Tigard "
                    "and is not readable from software")


async def console_send(session: str, data_b64: str) -> str:
    # Decode BEFORE touching the session, so a malformed payload cannot leave a
    # half-written line on the wire.
    try:
        payload = base64.b64decode(data_b64, validate=True)
    except Exception:                             # noqa: BLE001
        return _err("data_b64 is not valid base64")
    try:
        sess = await asyncio.to_thread(MANAGER.get, session)
    except KeyError:
        return _err(f"no such session {session!r}")
    if not sess.alive:
        return _err(f"session {session!r} is not alive: "
                    f"{getattr(sess, 'death_reason', 'unknown')}")
    try:
        # Off the event loop. `write` waits on `_write_lock`, which
        # `scan_baud` holds for an entire scan and `_shutdown` holds while it
        # drains -- and the wait is unbounded, since `write` acquires without
        # a timeout (session.py:369). Measured over a pty through this
        # handler: 3.78s blocked, and a 50 ms status poller completed 0 of
        # the ~75 polls due in that window. Task 7 made this move for the
        # scan and said it was "so a concurrent low-tier call (status, a
        # read) is not starved for the whole scan" -- true of the scan, and
        # false of the worker while the two calls that WAIT ON THE SCAN were
        # still inline.
        #
        # The liveness checks above are not re-done here on purpose:
        # `write` re-checks `_closed`/`_closing`/`alive` INSIDE `_write_lock`
        # (session.py:370-379), which is the only place the check is not
        # check-then-act. This handler's check is an early refusal, not the
        # guard.
        await asyncio.to_thread(sess.write, payload)
    except SessionError as exc:
        # Covers both a bounded write timeout (recoverable; the session stays
        # open) and a short write aborted by a concurrent close (the message
        # is a LOWER BOUND -- "at least N of M bytes" -- because pyserial's
        # abort path drops the final chunk from its count). Either way this
        # must not escape as an opaque transport failure, and `sent` must
        # never be reported here: the write did not demonstrably complete.
        return _err(str(exc))
    return _ok(session=session, sent=len(payload))


async def console_detect_baud(device: str | None = None,
                              rates: list[int] | None = None) -> str:
    """Sample the open session's line at each candidate rate. Never opens a port.

    RULING: `contract.py`'s `device` argument implies opening a port of its
    own, which collides with the one-exclusive-session rule and with never
    closing/reopening between candidates (a reopen re-asserts DTR). This
    requires an already-open session and scans on ITS held descriptor;
    `device`, if given, is only checked against that session's device for the
    caller's own sanity, never used to open anything.

    On a clear winner the session is LEFT at that rate -- no second open, so
    no second DTR assertion at the target. When nothing scores well, the
    session's rate is RESTORED to what it was: leaving a board at a rate that
    produced garbage is worse than leaving it where it started.
    """
    sess = await asyncio.to_thread(_manager_current)
    if sess is None:
        return _err(
            "no console session is open; call console_open first. "
            "console_detect_baud scans the open session's held descriptor "
            "and never opens a port of its own -- opening its own port would "
            "collide with the one-exclusive-session rule and would mean "
            "reopening (and re-asserting DTR) between candidates"
        )
    if device is not None and device != sess.device.by_id:
        return _err(
            f"session {sess.id} is open on {sess.device.by_id!r}, not "
            f"{device!r}. console_detect_baud scans the open session's port "
            "and does not open a different device -- close the current "
            "session first if you meant to scan a different one"
        )
    if not sess.alive:
        return _err(f"session {sess.id} is not alive, refusing to scan: "
                    f"{sess.death_reason}")

    try:
        candidate_rates = sanitize_rates(rates)
        check_budget(len(candidate_rates), DEFAULT_SAMPLE_SECONDS,
                     CONFIG.request_deadline_s)
    except BaudScanError as exc:
        return _err(str(exc))

    try:
        # The longest call in the worker: run the blocking scan off the event
        # loop so a concurrent low-tier call (status, a read) is not starved
        # for the whole scan.
        result = await asyncio.to_thread(
            sess.scan_baud, candidate_rates, DEFAULT_SAMPLE_SECONDS, pick_winner)
    except SessionError as exc:
        return _err(str(exc))

    samples = result["samples"]
    candidates = rank_candidates(samples)
    for candidate in candidates:
        candidate["sample_b64"] = base64.b64encode(candidate.pop("sample")).decode("ascii")

    response = dict(
        session=sess.id,
        original_baud=result["original_baud"],
        final_baud=result["final_baud"],
        restored=result["restored"],
        alive=result["alive"],
        death_reason=result["death_reason"],
        candidates=candidates,
        # A candidate the hardware itself rejected at apply time (not a
        # sanitize_rates/scan_baud ceiling refusal, which never reaches
        # here) -- named per rate, alongside whatever candidates DID get
        # sampled. Never thrown away wholesale over one bad rate. A list of
        # {rate, reason}, matching `candidates`' shape: session.py's
        # {rate: reason} dict would travel over JSON with its int keys
        # coerced to strings, so a caller correlating a rejected rate
        # against `candidates[]["rate"]` (an int) would see mismatched
        # types for the same kind of value in the same response.
        rejected=[{"rate": rate, "reason": reason}
                 for rate, reason in result["rejected"].items()],
        # This scan suspended the reader for the duration of the sampling --
        # a real hole in the boot log, not just an implementation detail.
        # Also visible via console_status (running count/seconds, plus
        # capture_suspended -- seconds alone reads as 0.0 for a gap still in
        # progress, which is why that flag exists) and console_read (flagged
        # on whichever read's window crosses it).
        capture_gap=result["gap"],
    )
    requested = len(candidate_rates)
    attempted = len(samples) + len(result["rejected"])
    # `death_reason` is the one liveness signal in this result that is NOT
    # racy, and it is why `alive` is still not consulted here. `_die` is the
    # only thing that ever sets it, and `_die` is only reached from a real
    # read/write/ioctl failure or a by-id path that stopped resolving --
    # whereas `_shutdown` sets `_alive = False` on a perfectly healthy
    # session (session.py:763-764) without touching `_death_reason`. So a
    # non-None `death_reason` means the DEVICE failed, never "a close was
    # racing this response".
    death_reason = result["death_reason"]
    if attempted < requested:
        # The loop stopped early (session.py: _stop/_closing observed
        # mid-scan -- a concurrent close, or the device disappearing) before
        # every candidate could even be tried. `alive` is not a safe signal
        # here: it is read after `scan_baud` releases `_write_lock`, which
        # is exactly what a racing `_shutdown` was waiting on, so it can
        # still read True in the instant this response is built. The
        # attempted-vs-requested COUNT is not racy -- `samples` and
        # `rejected` are populated by this one scan and nothing else -- so
        # it is what decides this, not `alive`. An aborted scan gets its own
        # verdict: falling through to "all candidates rejected" or "no data"
        # below would claim every candidate was tried when most never were.
        response["verdict"] = "scan_aborted"
        cause = (f"the device died mid-scan: {death_reason}"
                 if death_reason is not None
                 else "the session is closing or the device disappeared "
                      "mid-scan")
        response["note"] = (
            f"the scan stopped after {attempted} of {requested} candidate "
            f"rate(s) -- {cause}; ranked evidence and rejections cover only "
            "what was actually attempted"
        )
    elif death_reason is not None:
        # Every candidate was attempted, so none of the count-based verdicts
        # above fires -- but the device died somewhere in there, most often
        # on the LAST candidate, where nothing stopped the loop early. Left
        # to fall through, this would report a completed verdict (and, when
        # the dying candidates came back empty, the wiring hint) for a scan
        # whose evidence is about a link that failed partway. `alive: False`
        # and `death_reason` were already in the response; they were just
        # sitting next to a verdict that contradicted them.
        response["verdict"] = "scan_device_died"
        response["note"] = (
            f"every candidate rate was attempted, but the link failed during "
            f"the scan: {death_reason}. Samples taken after that point are "
            "evidence about a dead link, not about a baud rate -- fix the "
            "device and scan again rather than reading the ranking below as "
            "a verdict on the rates."
        )
    elif not samples and result["rejected"]:
        # Distinct from a genuinely silent line: nothing was ever sampled
        # because every candidate was refused before a byte could be read,
        # so the ground/TX-RX-crossover hint below would be actively
        # misleading -- the problem is the rate list, not the wiring.
        response["verdict"] = "all_candidate_rates_rejected"
        response["note"] = (
            "every candidate rate was rejected by this hardware before any "
            "sample could be taken; see \"rejected\" for why each one failed"
        )
    elif all_silent(samples):
        # The BENIGN half of the wiring pair: a wrong TX/RX crossover just
        # produces silence, and this verdict already reports that honestly.
        # The hint is attached because the check costs an operator nothing,
        # not because silence is the diagnostic case.
        response["verdict"] = "no_data_at_any_rate"
        response["note"] = (
            "no bytes arrived at any candidate rate -- the line was silent, "
            "not garbled"
        )
        response["hint"] = GROUND_CROSSOVER_HINT
    elif result["restored"]:
        response["verdict"] = "no_clear_winner"
        response["note"] = (
            "no candidate rate looked like clean console text; the session "
            f"was left at its original baud ({result['original_baud']})"
        )
        if all_scored_poorly(samples):
            # THE case this hint is for, and the one it used to miss. Bytes
            # did arrive, and every candidate scored below
            # WIRING_SUSPECT_PRINTABLE_MAX -- which is what a floating
            # ground looks like, and also what a wrong rate looks like,
            # because framing errors and a mis-framed rate produce the same
            # evidence. `score_sample` cannot separate them, so the note
            # above ("no candidate rate looked like clean console text")
            # would otherwise send the operator off to try more rates with
            # no indication that the wire is the other half of the
            # explanation.
            response["hint"] = GROUND_CROSSOVER_HINT
    else:
        response["verdict"] = "winner"
        response["note"] = (
            f"left the session at {result['final_baud']} baud -- no second "
            "open, and therefore no second DTR assertion at the target"
        )
    return _ok(**response)


def _device_summary(device) -> dict[str, Any]:
    # by_id/tty/serial/interface only -- NEVER voltage. The Tigard's level
    # selector is a physical slide switch with no software read-back,
    # unlike everything else here; a reported number is a guess an operator
    # could trust, and trusting a wrong one drives 5V into a 3.3V target.
    return {"by_id": device.by_id, "tty": device.tty,
            "serial": device.serial, "interface": device.interface}


async def list_devices() -> str:
    """List serial adapters present. Never reports voltage -- see module note.

    The scan is a `listdir` plus a `realpath` per entry on `/dev/serial/by-id`.
    Cheap, but they are blocking syscalls against a bus whose adapters can be
    half-unplugged, so they go to a thread like every other syscall here
    rather than being the one exception nobody revisits.
    """
    devices = await asyncio.to_thread(list_serial_devices)
    return _ok(devices=[_device_summary(d) for d in devices])


def _artifact_root_status(root: str | None) -> dict[str, Any]:
    """The artifact root's live state: present?, writable?, drive id.

    Read-only: `os.access` and a file read, never a write -- bench_status
    must answer cheaply with no session open and never dispatch anything
    that writes.
    """
    if not root or not os.path.isdir(root):
        return {"declared": root, "present": False, "writable": False,
                "drive_id": None}

    writable = os.access(root, os.W_OK)
    drive_id = None
    id_path = os.path.join(root, ".bench-store-id")
    if os.path.isfile(id_path):
        try:
            raw = open(id_path, "r").read().strip()
        except OSError:
            raw = ""
        if raw:
            # Sanitize rather than pass through: this value is about to
            # reach an operator's terminal in a bench_status response.
            drive_id = _CONTROL_RE.sub("", raw)

    return {"declared": root, "present": True, "writable": writable,
            "drive_id": drive_id}


async def bench_status() -> str:
    """Bench health with no session required: adapters, artifact root, drive id.

    §8.4 (see pare/commands/health.py) puts the live artifact-root answer
    behind this low-tier tool because the daemon resolving the path itself
    would resolve against the wrong machine's filesystem. Calls `load_config()`
    fresh on every call rather than reading the module-level `CONFIG`
    snapshot -- not for any live-reload reason (a running process's
    `os.environ` is fixed at exec time; changing the unit's `Environment=`
    needs `systemctl restart`, which re-execs and would refresh a frozen
    `CONFIG` just as well). The real reason is test-shaped: this tool's tests
    use `monkeypatch.setenv`, while `console_open`'s use
    `monkeypatch.setattr(tools, "CONFIG", ...)` -- a frozen singleton can't
    observe `setenv`. CONFIG-vs-`load_config()` is not yet one convention
    across this file; whoever next adds a field consumed by both `console_open`
    and `bench_status` needs to pick one rather than let the divergence grow.

    Never opens, closes or otherwise dispatches against a session -- `status`
    is read-only, matching this tool's low tier.
    """
    cfg = load_config()
    # `_artifact_root_status` stats and reads a file under an operator-declared
    # root -- in practice a removable drive on the bench. A `stat` on a wedged
    # or disconnected USB filesystem blocks in the kernel for as long as it
    # takes, and this is the tool an operator reaches for precisely when the
    # bench is misbehaving; it must not be able to take the worker's event
    # loop with it. `MANAGER.status()` goes with them: it takes the manager
    # lock, and `SessionManager.open` holds that lock for the whole of a port
    # open (session.py:875-920) -- which is itself on a thread now, so this
    # one can genuinely queue behind it.
    devices, artifact_root, session_status = await asyncio.gather(
        asyncio.to_thread(list_serial_devices),
        asyncio.to_thread(_artifact_root_status, cfg.artifact_root),
        asyncio.to_thread(MANAGER.status),
    )
    return _ok(
        devices=[_device_summary(d) for d in devices],
        artifact_root=artifact_root,
        session={
            "open": session_status["open"],
            "session": session_status["session"],
            "alive": session_status["alive"],
        },
    )
