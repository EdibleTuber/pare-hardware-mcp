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

from pare_hardware_mcp.baud import DEFAULT_SCAN_BUDGET_SECONDS
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
    # scan budget (`scan_budget_s` below -- a wall-clock budget the sweep
    # spends, NOT a per-candidate cost that grows with the rate list)
    # against before touching the port, refusing a scan that would
    # run past it rather than discovering the overrun as a transport-level
    # timeout. A candidate list too long to sweep even once is refused by the
    # same check, against the budget rather than against this. As of
    # 2026-09-20 this worker is declared `transport: streamable_http` in
    # PARE's `workers.yaml` and has its own `read_timeout: 60` declared
    # there — that number is the operator's authority; this default is a
    # local fallback when the worker is exercised out of process (tests,
    # manual runs). See PARE
    # `docs/superpowers/specs/2026-09-20-bench-integration-design.md` §4-5.
    request_deadline_s: float = 60.0
    # PARE_HW_SCAN_BUDGET_S: total wall time one `console_detect_baud` spends
    # sampling, across all sweeps. The default is what the sweep design says
    # catching a boot burst takes (baud.DEFAULT_SCAN_BUDGET_SECONDS), and
    # lowering it buys a scan that fits a tight deadline at the cost of
    # fewer sweeps -- less chance of a burst landing in any candidate's
    # window, which is the whole point of sweeping.
    #
    # It is an OPERATOR lever and deliberately not a tool argument, for the
    # same reason `expect_serial` is not: the caller is a language model,
    # and a model asked to "detect the baud rate quickly" would shorten the
    # budget and get back exactly the lucky-window ranking the sweep exists
    # to replace. It lives here because the conflict it resolves is between
    # two things only an operator sets -- this and
    # PARE_HW_REQUEST_DEADLINE_S. Before it existed, an operator whose
    # deadline was under the fixed 12s budget could not scan at all and had
    # no lever of any kind: the rate list no longer affects the budget, so
    # passing fewer rates did not help either.
    scan_budget_s: float = DEFAULT_SCAN_BUDGET_SECONDS
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
        scan_budget_s=_positive_float_env(
            "PARE_HW_SCAN_BUDGET_S", DEFAULT_SCAN_BUDGET_SECONDS),
        artifact_root=os.environ.get("PARE_HW_ARTIFACT_ROOT") or None,
        buffer_bytes=_positive_int_env(
            "PARE_HW_BUFFER_BYTES", DEFAULT_CAPACITY),
    )
