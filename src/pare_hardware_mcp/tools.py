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

from pare_hardware_mcp.ringbuffer import CursorError
from pare_hardware_mcp.session import SessionManager

MANAGER = SessionManager()


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
