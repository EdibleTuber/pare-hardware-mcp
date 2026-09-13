"""Worker configuration, read from the environment.

This worker is reached over HTTP: for that transport agent_core's daemon
forwards only `endpoint`, `connect_timeout` and `read_timeout` from
workers.yaml (see that file's "networked workers" section) -- `env` is a
stdio-only channel and is never plumbed through to a streamable_http worker.
So the systemd unit's `Environment=` is the only channel an operator has to
declare the by-id device path this worker should open, or the serial
`console_open` must assert before it ever touches a board.

A later task adds PARE_HW_ARTIFACT_ROOT and PARE_HW_BUFFER_BYTES here,
alongside the two below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


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
    # scan budget (len(rates) * sample_seconds) against before touching the
    # port, refusing a candidate list that would run past it rather than
    # discovering the overrun as a transport-level timeout. This worker is
    # still declared `transport: stdio` in workers.yaml (see the module
    # docstring), so there is no real `read_timeout` to read yet -- 60
    # matches the `read_timeout` already used by another networked worker's
    # entry there (frida), so this tracks a real precedent rather than an
    # invented number. An operator overrides it once the hardware worker
    # itself is networked and has its own `read_timeout` declared.
    request_deadline_s: float = 60.0


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


def load_config() -> Config:
    return Config(
        device=os.environ.get("PARE_HW_DEVICE") or None,
        expect_serial=os.environ.get("PARE_HW_EXPECT_SERIAL") or None,
        request_deadline_s=_positive_float_env(
            "PARE_HW_REQUEST_DEADLINE_S", 60.0),
    )
