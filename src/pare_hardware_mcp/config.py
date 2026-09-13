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


def load_config() -> Config:
    return Config(
        device=os.environ.get("PARE_HW_DEVICE") or None,
        expect_serial=os.environ.get("PARE_HW_EXPECT_SERIAL") or None,
    )
