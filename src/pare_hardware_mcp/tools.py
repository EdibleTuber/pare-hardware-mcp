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
import os
import re
from typing import Any

from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_SAMPLE_SECONDS,
                                    GROUND_CROSSOVER_HINT, all_silent,
                                    check_budget, pick_winner,
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

    A non-integer `limit` is refused rather than clamped: `min("4096", n)`
    raises `TypeError`, which no caller-facing except clause here catches, so
    it would escape the `{"error": ...}` contract as an opaque transport
    failure. The input schema says integer, but this worker's bind address is
    its only access control -- the schema is not a guarantee.
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
        sess = MANAGER.get(session)
    except KeyError:
        return _err(f"no such session {session!r}")
    try:
        data, next_cursor, dropped, remaining = sess.buffer.read(cursor, capped)
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


def _device_summary(device) -> dict[str, Any]:
    # by_id/tty/serial/interface only -- NEVER voltage. The Tigard's level
    # selector is a physical slide switch with no software read-back,
    # unlike everything else here; a reported number is a guess an operator
    # could trust, and trusting a wrong one drives 5V into a 3.3V target.
    return {"by_id": device.by_id, "tty": device.tty,
            "serial": device.serial, "interface": device.interface}


async def list_devices() -> str:
    """List serial adapters present. Never reports voltage -- see module note."""
    return _ok(devices=[_device_summary(d) for d in list_serial_devices()])


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
    session_status = MANAGER.status()
    return _ok(
        devices=[_device_summary(d) for d in list_serial_devices()],
        artifact_root=_artifact_root_status(cfg.artifact_root),
        session={
            "open": session_status["open"],
            "session": session_status["session"],
            "alive": session_status["alive"],
        },
    )
