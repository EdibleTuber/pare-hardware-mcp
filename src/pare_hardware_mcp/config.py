"""Worker configuration, read from the environment.

This worker is reached over HTTP: for that transport agent_core's daemon
forwards only `endpoint`, `connect_timeout` and `read_timeout` from
workers.yaml (see that file's "networked workers" section) -- `env` is a
stdio-only channel and is never plumbed through to a streamable_http worker.
So the systemd unit's `Environment=` is the only channel an operator has to
declare the by-id device path this worker should open, or the serial
`console_open` must assert before it ever touches a board.

"""
from __future__ import annotations

import os
from dataclasses import dataclass

from pare_hardware_mcp.ringbuffer import DEFAULT_CAPACITY


@dataclass(frozen=True)
class Config:
    # PARE_HW_DEVICE: default by-id path `console_open` uses when the caller
    # (a language model) omits `device`. `None` when unset -- there is no
    # sane default board to guess at.
    device: str | None = None
    # PARE_HW_EXPECT_SERIAL: the serial `console_open` asserts against the
    # resolved device. Deliberately not a tool argument: the caller is a
    # language model, and letting it supply the expected serial would defeat
    # the check -- the point is that an OPERATOR declares it out of band.
    expect_serial: str | None = None
    # PARE_HW_REQUEST_DEADLINE_S: the deadline `console_detect_baud` checks its
    # scan budget (baud.DEFAULT_SCAN_BUDGET_SECONDS -- a fixed wall-clock
    # budget the sweep spends, NOT a per-candidate cost that grows with the
    # rate list) against before touching the port, refusing a scan that would
    # run past it rather than discovering the overrun as a transport-level
    # timeout. A candidate list too long to sweep even once is refused by the
    # same check, against the budget rather than against this. This worker is
    # still declared `transport: stdio` in workers.yaml (see the module
    # docstring), so there is no real `read_timeout` to read yet -- 60
    # matches the `read_timeout` already used by another networked worker's
    # entry there (frida), so this tracks a real precedent rather than an
    # invented number. An operator overrides it once the hardware worker
    # itself is networked and has its own `read_timeout` declared.
    request_deadline_s: float = 60.0
    # PARE_HW_ARTIFACT_ROOT: the directory `bench_status` inspects for
    # presence, writability and a `.bench-store-id` drive id. Not opened,
    # written to, or otherwise dispatched against by phase 1 -- artifacts are
    # out of scope (see contract.py) -- so `None` when unset is a normal
    # "this worker has no artifact root declared" state, not an error.
    artifact_root: str | None = None
    # PARE_HW_BUFFER_BYTES: worker-configurable capture-buffer capacity. The
    # design derives 64 MiB (ringbuffer.DEFAULT_CAPACITY) as the default from
    # observed boot-log volume; an operator with a more talkative target or a
    # tighter memory budget overrides it here.
    buffer_bytes: int = DEFAULT_CAPACITY


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be positive")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if value <= 0:
        raise ValueError(f"{name}={raw!r} must be positive")
    return value


def load_config() -> Config:
    return Config(
        device=os.environ.get("PARE_HW_DEVICE") or None,
        expect_serial=os.environ.get("PARE_HW_EXPECT_SERIAL") or None,
        request_deadline_s=_positive_float_env(
            "PARE_HW_REQUEST_DEADLINE_S", 60.0),
        artifact_root=os.environ.get("PARE_HW_ARTIFACT_ROOT") or None,
        buffer_bytes=_positive_int_env(
            "PARE_HW_BUFFER_BYTES", DEFAULT_CAPACITY),
    )
