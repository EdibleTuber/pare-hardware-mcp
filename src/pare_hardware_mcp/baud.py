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
    """Evidence about whether `data` looks like console output at this rate.

    KNOWN GAP -- `framing_errors` IS SPECIFIED AND IS NOT MEASURED HERE.
    The spec asks for a `framing_errors` count alongside these fields;
    `nulls` went in instead, and the substitution was never recorded anywhere
    a reader of this function would find it. Nothing in this package claims
    framing errors are measured, so this is a missing statement rather than a
    false one -- but it is missing from the one place it matters.

    Deferred rather than guessed: a framing-error count is only useful with a
    threshold, and calibrating one needs a real adapter against a real target
    with a deliberately floated ground. A number invented at a desk here
    would be exactly the confident-and-wrong verdict this module exists to
    avoid.

    The consequence, while it is absent: `printable_ratio` is the ONLY
    discriminator `WIRING_SUSPECT_PRINTABLE_MAX` has, which is part of why
    that constant is set where it is -- see its docstring. A real
    framing-error count is what would finally separate "the ground is
    floating" from "the rate is wrong", which `printable_ratio` provably
    cannot.
    """
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

WIRING_SUSPECT_PRINTABLE_MAX = 0.5
"""Below this `printable_ratio`, a candidate is not "a worse rate" -- it is
evidence that nothing on this line resembled text at that speed.

Three reference points fix it, and the gaps between them are wide:

- A floating ground, or TX/RX swapped, yields high-bit noise or nothing:
  `printable_ratio` 0.0. (Reproduced against a pty fed `bytes(range(128,
  256))` -- a floating ground's signature -- where every candidate scored
  0.0.)
- A merely WRONG rate reframes real traffic into approximately uniform
  bytes. 100 of the 256 byte values are in `string.printable`, so that
  lands near 0.39.
- Real console text scores > 0.9, and `WINNER_PRINTABLE_THRESHOLD` (0.85)
  is the bar it must clear to be left live.

0.5 sits ABOVE the wrong-rate expectation, so both the 0.0 case and the
0.39 case fall under it -- and that is the point, not an accident. Those two
are precisely the pair `score_sample` cannot tell apart, which is the whole
reason the hint exists: framing errors from a floating ground and a rate
that reframes real traffic produce the same shape of evidence. A scan where
every candidate lands in that band has not established which one it is, and
`DEFAULT_RATES` already covers every common console speed, so "the rate list
was wrong" is the less likely half of the pair by the time all eight have
failed.

What 0.5 keeps OUT is the marginal candidate: 0.6-0.84 is plausible text
with some corruption -- a rate near the right one, worth retrying -- and
that must not draw a wiring accusation.

It is deliberately NOT the complement of `WINNER_PRINTABLE_THRESHOLD`:
"nothing scored well enough to win" (anything under 0.85) is the ordinary
outcome of a scan that simply missed the right rate, and firing on every one
of those would train a caller to ignore the hint.

This bar carries more weight than it should have to, because the spec's
`framing_errors` field was never implemented -- see `score_sample`. Until it
is, `printable_ratio` is the only discriminator this hint has.

Which way to err was decided by the costs. A false positive costs an
operator one look at a ground wire. A false negative is the defect this
constant exists to fix: the scan confidently blames the rate, and the
operator spends the next hour on rates while the fault is the wire. The
ranked evidence is in the same response either way -- this only ever ADDS a
`hint`, it never replaces a verdict or changes a score.
"""

GROUND_CROSSOVER_HINT = (
    "Before trying more rates, check the wiring: "
    "(1) ground is connected between the adapter and the target -- a floating "
    "ground produces framing errors that look exactly like a wrong baud rate, "
    "so the scan cannot tell the two apart and will otherwise blame the rate; "
    "(2) TX/RX are not swapped -- the adapter's TX must reach the target's RX "
    "and vice versa."
)
"""Invariant 6: name the physical causes, rather than just "try more rates".

Deliberately says nothing about WHICH symptom was observed -- the verdict and
its note carry that -- because the two symptoms that warrant it are different
and only one of them is silence. A wrong crossover just produces silence,
which `no_data_at_any_rate` already reports honestly; a floating ground
produces framing errors that score exactly like a wrong baud rate, and that
is the case where the hint changes what an operator does.
"""


def all_scored_poorly(samples: dict[int, bytes]) -> bool:
    """True when EVERY candidate scored below `WIRING_SUSPECT_PRINTABLE_MAX`.

    The condition invariant 6's hint is really about. A floating ground
    produces framing errors, framing errors look exactly like a wrong baud
    rate to `score_sample`, and the ranking will otherwise hand back a
    confident "no candidate rate looked like clean console text" that sends
    the operator off to try more rates while the fault is a wire.

    An empty `samples` is NOT "everything scored poorly" -- nothing was
    scored at all. That case is `all_silent`, or `all_candidate_rates_
    rejected` when every rate was refused before a byte could be read, and
    both have their own verdict; `all(...)` over an empty dict would return
    True and quietly annex them.
    """
    if not samples:
        return False
    return all(
        score_sample(data)["printable_ratio"] < WIRING_SUSPECT_PRINTABLE_MAX
        for data in samples.values()
    )


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
