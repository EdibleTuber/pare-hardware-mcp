"""Baud detection: scoring, and the candidate list.

The SCAN itself lives on `ConsoleSession` (`session.py`): it needs the held
descriptor, and getting its ordering wrong reboots the operator's target
repeatedly, which is not something a pure module can be trusted with. This
module is the hardware-free half -- scoring a sample, sanitising a candidate
list, checking the scan's time budget, and ranking/deciding once samples come
back. A wrong rate produces plausible garbage rather than an error, which is
why callers get ranked scores and never a bare verdict.
"""
from __future__ import annotations

import string

DEFAULT_RATES: tuple[int, ...] = (
    9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600,
)
"""Common console speeds, ascending.

ZERO IS NOT AND MUST NOT BE A CANDIDATE. POSIX defines setting the output baud
rate to zero as deasserting the modem control lines; on ftdi_sio that drops DTR
and RTS, which is a target reset reachable from a tier-`low` tool. Any
caller-supplied list is filtered against this before the port is touched.
"""

_PRINTABLE = frozenset(
    (string.printable).encode("ascii")
)


def score_sample(data: bytes) -> dict:
    """Evidence about whether `data` looks like console output at this rate."""
    if not data:
        return {"printable_ratio": 0.0, "has_crlf": False, "nulls": 0}
    printable = sum(1 for b in data if b in _PRINTABLE)
    return {
        "printable_ratio": printable / len(data),
        "has_crlf": b"\r\n" in data,
        "nulls": data.count(0),
    }


# -- everything below is orchestration support, not part of the brief's ------
# -- plan-supplied code -- but it is pure (no hardware, no I/O) and unit --
# -- tested the same way. -----------------------------------------------

DEFAULT_SAMPLE_SECONDS = 1.5
"""Per-candidate listen time.

At 9600 baud (the slowest default candidate) that is ~1440 bytes -- 20-30
lines of boot text, comfortably enough for `score_sample` to tell readable
console output from noise. Not a tool argument: `contract.py`'s schema for
`console_detect_baud` takes only `device` and `rates`, so this is the one
knob the budget check (below) reasons about.
"""

WINNER_PRINTABLE_THRESHOLD = 0.85
"""How good a candidate's `printable_ratio` must be to be left live.

Above the brief's own "bad" fixture (high-bit noise, < 0.2) by a wide margin,
and just under its "good" fixture (real boot text, > 0.9): a candidate has to
look unambiguously like text, not merely better than its neighbours. Without
an absolute bar, a scan where every rate is equally garbled -- a floating
ground, or TX/RX swapped -- would still crown a "winner" by comparison alone
and leave the port at an arbitrary rate.
"""

SAMPLE_PREVIEW_BYTES = 256
"""How much of each candidate's raw sample travels in the result.

Invariant 5 asks for "a short sample" alongside every candidate's scores, not
the whole capture -- this keeps the tool result compact for every candidate
rate at once rather than only the winner.
"""

GROUND_CROSSOVER_HINT = (
    "no bytes arrived at any candidate rate. Before trying more rates, check: "
    "(1) ground is connected between the adapter and the target -- a floating "
    "ground produces framing errors that look exactly like a wrong baud rate; "
    "(2) TX/RX are not swapped -- the adapter's TX must reach the target's RX "
    "and vice versa."
)
"""Invariant 6: a silent line names physical causes, not just "try more rates"."""


class BaudScanError(RuntimeError):
    """A scan request was refused before the port was ever touched."""


MAX_BAUD_RATE = 4_000_000
"""Ceiling for a caller-supplied rate list.

pyserial's custom-baud path stores the rate in a C `int` (`array('i')`) on
Linux: a rate at or above 2**31 raises `OverflowError` from inside termios
rather than refusing cleanly, and a rate the kernel rejects for the specific
chip is converted to `ValueError`, not `SerialException` -- neither is a
failure `scan_baud` can safely leave unhandled from a tier-low,
never-prompted tool whose `rates` argument is an unconstrained integer
array. 4 Mbaud is comfortably above every real UART console speed (the
highest `DEFAULT_RATES` candidate is 921 600) and comfortably below the
overflow boundary, so this rejects the class of input that reaches it
without rejecting anything a real board could plausibly use.
"""


def sanitize_rates(rates) -> tuple[int, ...]:
    """`DEFAULT_RATES` when the caller supplies nothing; otherwise filtered.

    Invariant 2: 0 -- and anything else that is not a positive integer -- is
    dropped here, before any candidate list reaches the port. Refuses only
    when NOTHING usable remains, so a caller who accidentally includes a 0
    alongside real candidates gets a scan of the rest rather than a bare
    refusal.
    """
    if rates is None:
        return DEFAULT_RATES
    cleaned: list[int] = []
    seen: set[int] = set()
    for r in rates:
        if not isinstance(r, int) or isinstance(r, bool):
            continue
        if not (0 < r <= MAX_BAUD_RATE):
            continue
        if r in seen:
            continue
        seen.add(r)
        cleaned.append(r)
    if not cleaned:
        raise BaudScanError(
            f"no usable candidate rates in {rates!r}: every value was "
            "non-positive (0 is refused -- POSIX reads it as 'deassert the "
            f"modem control lines', which drops DTR/RTS), above the "
            f"{MAX_BAUD_RATE} ceiling, or not an integer"
        )
    return tuple(cleaned)


def check_budget(num_rates: int, sample_seconds: float, deadline_s: float) -> None:
    """Invariant 4: refuse an overrun candidate list, don't discover it as a timeout."""
    budget = num_rates * sample_seconds
    if budget > deadline_s:
        raise BaudScanError(
            f"scanning {num_rates} candidate rate(s) at {sample_seconds}s "
            f"each needs {budget:.1f}s, which exceeds this worker's "
            f"{deadline_s:.1f}s request deadline; pass fewer rates"
        )


def all_silent(samples: dict[int, bytes]) -> bool:
    """True when the line produced nothing at any candidate rate."""
    return not samples or all(len(data) == 0 for data in samples.values())


def pick_winner(samples: dict[int, bytes]) -> int | None:
    """The rate to leave the port at, or `None` to restore the original.

    Ranks by (printable_ratio, has_crlf, fewer nulls) and only accepts the
    best candidate if it also clears `WINNER_PRINTABLE_THRESHOLD` -- see that
    constant for why a merely-best-of-a-bad-lot candidate must not win.
    """
    best_rate: int | None = None
    best_key: tuple | None = None
    for rate, data in samples.items():
        if not data:
            continue
        scored = score_sample(data)
        key = (scored["printable_ratio"], scored["has_crlf"], -scored["nulls"])
        if best_key is None or key > best_key:
            best_key, best_rate = key, rate
    if best_rate is None:
        return None
    if score_sample(samples[best_rate])["printable_ratio"] < WINNER_PRINTABLE_THRESHOLD:
        return None
    return best_rate


def rank_candidates(samples: dict[int, bytes]) -> list[dict]:
    """Every candidate, scored, best-first. Invariant 5: ranked evidence, never a verdict.

    Each entry carries the raw (untruncated only up to `SAMPLE_PREVIEW_BYTES`)
    sample bytes under `"sample"` -- callers over the wire (`tools.py`) must
    base64-encode it themselves, the same way every other target-byte field in
    this worker travels, before it reaches an untrusted context.
    """
    candidates = []
    for rate, data in samples.items():
        scored = score_sample(data)
        candidates.append({
            "rate": rate,
            "bytes_captured": len(data),
            "sample": data[:SAMPLE_PREVIEW_BYTES],
            "sample_truncated": len(data) > SAMPLE_PREVIEW_BYTES,
            **scored,
        })
    candidates.sort(
        key=lambda c: (c["printable_ratio"], c["has_crlf"], -c["nulls"]),
        reverse=True,
    )
    return candidates
