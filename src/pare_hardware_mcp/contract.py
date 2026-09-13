"""What this worker exposes, and at what risk tier.

NAMES ARE BARE. The daemon prefixes with the workers.yaml key, so `console_send`
here is dispatched and pinned as `hardware_console_send`. Writing the prefix in
this file produces `hardware_hardware_console_send` on the wire, and the operator
pin -- the only control on the one dangerous tool in phase 1 -- matches nothing,
with no error anywhere.

NOTHING HERE MAY IMPORT HARDWARE. PARE's CI imports this module on a runner with
no Pi and no pyserial in order to validate risk pins against real tool names. An
import that reaches a device turns that check into a silent skip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pare_worker_kit import PRODUCES_RESULT

# `ringbuffer` imports nothing at all -- it does not reach pyserial and it
# does not reach a device -- so this stays inside the no-hardware rule
# above. Imported rather than retyped so the description below cannot drift
# away from the number `tools.py` actually enforces.
from pare_hardware_mcp.ringbuffer import DEFAULT_READ_LIMIT, MAX_READ_LIMIT

CONTRACT_VERSION = 1

_OBJ: dict[str, Any] = {"type": "object", "properties": {}}
_SUMMARY_OUT: dict[str, Any] = {"type": "object", "properties": {
    "summary": {"type": "string"},
}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk_tier: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: dict(_SUMMARY_OUT))
    produces: str = PRODUCES_RESULT


def _in(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("list_devices", "low",
             "List serial adapters present, by stable by-id path and serial "
             "number. Target VOLTAGE IS OPERATOR-SET on a physical switch and "
             "is not readable from software -- it is never reported here.",
             dict(_OBJ)),
    ToolSpec("bench_status", "low",
             "Bench health this worker can see: adapters present, artifact "
             "root present and writable, drive id. Cheap, no session needed.",
             dict(_OBJ)),
    ToolSpec("console_detect_baud", "low",
             "Detect the rate of a CHATTERING line -- one that is producing "
             "output throughout the scan window. Samples at each candidate "
             "rate and returns them RANKED WITH SCORES, not a verdict: a "
             "wrong rate yields plausible garbage rather than an error, so "
             "the caller must see the evidence. IT WILL NOT RELIABLY CATCH A "
             "BOOT BURST: a board's boot output can be milliseconds of wire "
             "time at a high rate, and a scan that samples one rate at a time "
             "is usually listening elsewhere when it lands. If you already "
             "know the rate, pass it to console_open rather than scanning. A "
             "silent line reports no-data-at-any-rate rather than guessing.",
             _in(device={"type": "string"},
                 rates={"type": "array", "items": {"type": "integer"}})),
    ToolSpec("console_open", "medium",
             "Open the console and START CAPTURING. Tier medium because this "
             "is not a read-only act: opening asserts DTR/RTS and many boards "
             "reset on DTR.",
             _in(device={"type": "string"}, baud={"type": "integer"},
                 flow={"type": "string"}, dtr={"type": "boolean"},
                 rts={"type": "boolean"})),
    ToolSpec("console_read", "low",
             "Read captured bytes since a cursor. Returns base64 (UART output "
             "is arbitrary bytes and is untrusted), the next cursor, how many "
             "bytes were LOST to buffer wrap, and how many remain unread. "
             "`limit` is a BYTE COUNT and is bounded: it defaults to "
             f"{DEFAULT_READ_LIMIT} and is clamped to at most {MAX_READ_LIMIT}, "
             "so one call can never return the whole capture buffer. The "
             "applied value comes back as `limit_applied`; when `remaining` "
             "is non-zero, call again with `next_cursor` to drain the rest.",
             _in(session={"type": "string"}, cursor={"type": "integer"},
                 limit={"type": "integer"})),
    ToolSpec("console_send", "high",
             "Send bytes to the target. PINNED high in workers.yaml: a console "
             "at a bootloader prompt can write flash.",
             _in(session={"type": "string"}, data_b64={"type": "string"})),
    ToolSpec("console_status", "low",
             "Session state: open?, device, baud, flow, session age, buffer "
             "head, and whether anything has been dropped.",
             dict(_OBJ)),
    ToolSpec("console_close", "low",
             "Release the port and end the session. The buffer is discarded; "
             "read what you need first.",
             _in(session={"type": "string"})),
]
