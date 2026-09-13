"""Tool handlers. Names match contract.py exactly; server.py binds by name.

TARGET BYTES ARE UNTRUSTED AND TRAVEL BASE64. UART output is arbitrary bytes,
not text, and it reaches the model's context, the capture store, /snapshot and
-- via the next call's argument snapshot -- the operator's approval prompt. The
artifact design applies a control-character check to `path` and `drive_id` for
exactly this reason; this is the same problem with far more volume. Encode here,
strip at render time, and keep the capture byte-exact because it is evidence.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_SAMPLE_SECONDS,
                                    GROUND_CROSSOVER_HINT, all_silent,
                                    check_budget, pick_winner,
                                    rank_candidates, sanitize_rates)
from pare_hardware_mcp.config import load_config
from pare_hardware_mcp.ringbuffer import CursorError
from pare_hardware_mcp.session import SessionError, SessionManager

MANAGER = SessionManager()
CONFIG = load_config()


def _ok(**fields: Any) -> str:
    return json.dumps(fields)


def _err(message: str) -> str:
    return json.dumps({"error": message})


async def console_read(session: str, cursor: int = 0,
                       limit: int | None = None) -> str:
    try:
        sess = MANAGER.get(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    try:
        data, next_cursor, dropped, remaining = sess.buffer.read(cursor, limit)
    except CursorError:
        # `cursor` is negative or ahead of `head`: not a position this API
        # ever handed out, so it can never be a legitimate "fell behind"
        # case. Let it escape and it becomes an opaque transport-level
        # failure instead of something the model can read and correct.
        return _err(f"invalid cursor {cursor} for session {session!r}")
    # `dropped` only ever counts bytes evicted by ring-buffer wraparound --
    # it cannot see a capture that was SUSPENDED (a baud scan pausing the
    # reader), because that leaves no byte range in cursor space at all,
    # only a hole in time. `capture_gaps` is the other half: any suspension
    # whose recorded cursor position falls inside the window just returned,
    # so this byte-contiguous stream does not read as an unbroken one to
    # whatever -- a language model, most likely -- consumes it next.
    capture_gaps = sess.gaps_overlapping(next_cursor - len(data), next_cursor)
    return _ok(session=session,
               data_b64=base64.b64encode(data).decode("ascii"),
               next_cursor=next_cursor, dropped=dropped, remaining=remaining,
               alive=sess.alive, capture_gaps=capture_gaps)


async def console_status() -> str:
    return _ok(**MANAGER.status())


async def console_close(session: str) -> str:
    try:
        MANAGER.close(session)
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
        sess = MANAGER.open(device=device, expect_serial=CONFIG.expect_serial,
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
        sess = MANAGER.get(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    if not sess.alive:
        return _err(f"session {session!r} is not alive: "
                    f"{getattr(sess, 'death_reason', 'unknown')}")
    try:
        sess.write(payload)
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
    sess = MANAGER.current
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
        response["note"] = (
            f"the scan stopped after {attempted} of {requested} candidate "
            "rate(s) -- the session is closing or the device disappeared "
            "mid-scan; ranked evidence and rejections cover only what was "
            "actually attempted"
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
        response["verdict"] = "no_data_at_any_rate"
        response["hint"] = GROUND_CROSSOVER_HINT
    elif result["restored"]:
        response["verdict"] = "no_clear_winner"
        response["note"] = (
            "no candidate rate looked like clean console text; the session "
            f"was left at its original baud ({result['original_baud']})"
        )
    else:
        response["verdict"] = "winner"
        response["note"] = (
            f"left the session at {result['final_baud']} baud -- no second "
            "open, and therefore no second DTR assertion at the target"
        )
    return _ok(**response)
