"""Tool handlers. Names match contract.py exactly; server.py binds by name.

TARGET BYTES ARE UNTRUSTED AND TRAVEL BASE64. UART output is arbitrary bytes,
not text, and it reaches the model's context, the capture store, /snapshot and
-- via the next call's argument snapshot -- the operator's approval prompt. The
artifact design applies a control-character check to `path` and `drive_id` for
exactly this reason; this is the same problem with far more volume. Encode here,
strip at render time, and keep the capture byte-exact because it is evidence.
"""
from __future__ import annotations

import base64
import json
from typing import Any

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
    return _ok(session=session,
               data_b64=base64.b64encode(data).decode("ascii"),
               next_cursor=next_cursor, dropped=dropped, remaining=remaining,
               alive=sess.alive)


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
