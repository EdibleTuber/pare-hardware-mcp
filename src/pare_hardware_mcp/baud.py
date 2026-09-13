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
    1152000, 1500000, 3000000,
)
"""Common console speeds, ascending.

ZERO IS NOT AND MUST NOT BE A CANDIDATE. POSIX defines setting the output baud
rate to zero as deasserting the modem control lines; on ftdi_sio that drops DTR
and RTS, which is a target reset reachable from a tier-`low` tool. Any
caller-supplied list is filtered against this before the port is touched.

THE TOP THREE WERE ADDED FROM A BENCH FAILURE, not from a catalogue. A Tigard
(FT2232H) against a Rockchip board found nothing: that SoC family's standard
console rate is 1 500 000, the list stopped at 921 600, and the scan could
not reach it AT ALL. What the operator got was not "rate not found" -- every
candidate scored like noise, which is also what a floating ground looks like,
so the run ended by recommending a check of wiring that was already correct.
A ladder that cannot reach a whole SoC family does not fail politely; it
fails confidently and wrongly, which is the one outcome this module exists to
prevent.

Why these three and not a longer ladder: the scan sweeps the whole list
repeatedly inside a fixed budget (see `DEFAULT_SCAN_BUDGET_SECONDS`), so every
added candidate takes dwell time away from all the others. These are the rates
an ordinary reverse-engineering target actually boots at above 921 600 --
1 500 000 on Rockchip (measured, this bench), 1 152 000 on Amlogic and several
Broadcom parts, 3 000 000 on Allwinner and as the FTDI-native top end. Rates
like 1 000 000 and 2 000 000 exist on debug headers but are not standard
CONSOLE rates for those families, and `rates` is a caller-supplied argument on
`console_detect_baud`: a caller who knows the part can pass them, and pays the
dilution only when it is warranted.
"""

_PRINTABLE = frozenset(
    (string.printable).encode("ascii")
)

# Byte-class lookup tables. `bytes.translate` + `bytes.count` does the counting
# in C, which matters now that a scan SWEEPS: a candidate's sample is scored
# once per sweep and grows each time, and at 3 Mbaud a whole scan can put a
# few MB through here. Measured: ~58 ms for 3 MB, against ~2 s for the
# equivalent Python-level `sum(1 for b in data ...)`.
_PRINTABLE_TABLE = bytes(1 if b in _PRINTABLE else 0 for b in range(256))
_TEXT_TABLE = bytes(
    1 if (b < 128 and (chr(b).isalnum() or b == 0x20)) else 0
    for b in range(256)
)

SPACE_RATIO_REFERENCE = 0.10
"""The space frequency genuine console output clears, used to NORMALISE.

The single most reliable separator found, and the one that survives console
output with no words in it at all. Console text -- boot logs, hexdumps,
shell sessions, bootloader banners -- is column- and token-separated, so
0x20 is its most common byte by a wide margin: measured 0.105-0.33 across
every genuine fixture in `tests/unit/_uart.py`, including a hexdump and a
timestamp-only kernel log, both of which have ZERO runs of three letters.

Misframed bytes do not concentrate on any single value: re-sampling a fast
waveform at a slow clock spreads the result across the reachable byte values
roughly evenly, so no value -- 0x20 included -- gets more than a few percent.
Measured 0.000-0.036 across 74 physically simulated resamplings of a real
1.5 Mbaud boot log at every wrong candidate rate.

Used as `min(1.0, space_ratio / SPACE_RATIO_REFERENCE)`, so clearing it earns
no extra credit: a chatty log with 25% spaces and a terse one with 11% score
the same, and only a sample BELOW the reference is penalised, in proportion.
That is deliberate -- a ratio that rewarded excess would make the score
depend on how verbose the target happens to be.
"""

MAX_PLAUSIBLE_LINE_LENGTH = 200.0
"""Bytes per line a console is expected to stay under, used to NORMALISE.

Console output is line-oriented; misframed bytes are not. `\\r\\n` is the
strongest single piece of evidence there is, because it is a TWO-byte
sequence: for bytes distributed even roughly uniformly it appears about once
in 16 000, where real console text emits one every 40-80 bytes. Measured: 0
occurrences in every one of the 74 simulated misframings, against 74 in the
3378-byte genuine log.

Same normalising shape as `SPACE_RATIO_REFERENCE`: a sample is expected to
carry at least `len(data) / MAX_PLAUSIBLE_LINE_LENGTH` terminators, and more
than that earns nothing extra. 200 is deliberately generous -- a target that
prints 150-byte lines must not be penalised for it.

`\\n` alone is the FALLBACK, used only when there is no `\\r\\n` anywhere,
because a single byte at 1-in-128 is far too common in garbage to lean on.
The fallback exists because an LF-only console is a real configuration, and
scoring it zero would be a false negative on working hardware.
"""


def score_sample(data: bytes) -> dict:
    """Evidence about whether `data` looks like console output at this rate.

    `console_score` is the discriminator; `printable_ratio` is kept as
    evidence and as a gate, but IT IS NOT THE DISCRIMINATOR ANY MORE, and
    the reason is a measurement rather than an argument. The analysis this
    module shipped with reasoned that a wrong rate reframes traffic into
    approximately uniform bytes, which would score ~0.39 printable. On real
    hardware -- a Tigard against a Rockchip board at 115200 while the target
    talked at 1500000 -- the garbage measured 0.75, against 0.911 for the
    genuine winner. Simulating that resampling physically (see
    `tests/unit/_uart.py`) reproduces the whole range: misframed
    `printable_ratio` lands anywhere in 0.00-0.79 depending only on the
    inter-byte timing of the source. It is not a weak discriminator by a
    constant factor, it is an unstable one.

    So the score is a product of three normalised factors, each of which
    genuine console output clears and misframed bytes do not:

        console_score = text_ratio * space_factor * line_factor

    - `text_ratio`  -- alphanumerics and spaces, the alphabet console output
      is actually made of, rather than "anything in string.printable".
    - `space_factor` -- see `SPACE_RATIO_REFERENCE`. Concentration on one
      byte value, which resampled noise cannot produce.
    - `line_factor` -- see `MAX_PLAUSIBLE_LINE_LENGTH`. Line structure,
      which resampled noise also cannot produce.

    They MULTIPLY rather than average on purpose: each one is necessary, and
    a sample that fails any of them is not console output however well it
    does on the other two. That is what stops the obvious adversary --
    printable-looking bytes with plausible `\\r\\n` sprinkled in -- which
    scores 0.788 printable and 0.044 console.

    Measured separation, all reproduced by `tests/unit/test_baud.py`:

    - genuine console output, ten varieties, 256 B to 4.7 KB:  >= 0.744
    - the same log misframed, 74 physical resamplings:         <= 0.074
    - engineered noise at the bench's 0.78 printable + CRLF:      0.044
    - high-bit noise (a floating ground) and a silent line:       0.000

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
    avoid. What has changed since that was written is only how much rides on
    its absence: `printable_ratio` is no longer the ONLY discriminator, so a
    floating ground is no longer distinguished from a wrong rate by one
    threshold on one weak number. It is still not distinguished -- both
    score near zero on `console_score` -- which is why
    `GROUND_CROSSOVER_HINT` still names both, and why it now also names the
    third member of that family, a target whose rate is not on the list at
    all. A real framing-error count is what would finally separate them.

    KNOWN FALSE NEGATIVE: console output containing no line terminator at
    all in the whole sample -- a bootloader prompt echoing `=> ` and nothing
    else -- scores 0.0 and cannot win. The scan then restores the original
    rate and hands back ranked evidence, which is the fail-safe direction:
    no rate is left applied on the strength of a sample with no structure in
    it. Sweeping makes this rarer than it looks, since a candidate's sample
    is accumulated across the whole budget rather than one short window.
    """
    n = len(data)
    if not n:
        return {
            "printable_ratio": 0.0, "has_crlf": False, "nulls": 0,
            "text_ratio": 0.0, "space_ratio": 0.0,
            "crlf_lines": 0, "lf_lines": 0,
            "mean_line_length": None, "line_structure": 0.0,
            "console_score": 0.0,
        }
    printable = data.translate(_PRINTABLE_TABLE).count(1)
    text = data.translate(_TEXT_TABLE).count(1)
    spaces = data.count(0x20)
    crlf_lines = data.count(b"\r\n")
    lf_lines = data.count(0x0A)
    terminators = crlf_lines if crlf_lines else lf_lines
    expected = max(1.0, n / MAX_PLAUSIBLE_LINE_LENGTH)
    line_structure = min(1.0, terminators / expected)
    text_ratio = text / n
    space_ratio = spaces / n
    space_factor = min(1.0, space_ratio / SPACE_RATIO_REFERENCE)
    return {
        "printable_ratio": printable / n,
        "has_crlf": crlf_lines > 0,
        "nulls": data.count(0),
        "text_ratio": text_ratio,
        "space_ratio": space_ratio,
        "crlf_lines": crlf_lines,
        "lf_lines": lf_lines,
        "mean_line_length": (n / terminators) if terminators else None,
        "line_structure": line_structure,
        "console_score": text_ratio * space_factor * line_structure,
    }


def sort_key(scored: dict) -> tuple:
    """Rank order, best first. `console_score` leads; the rest break ties.

    The older fields stay in the key underneath it rather than being dropped:
    when several candidates score 0.0 -- a scan where nothing had line
    structure, which is the common shape of a genuinely failed scan -- the
    ranking would otherwise be arbitrary, and a caller reading ranked
    evidence deserves the least-bad candidate first even when none is good.
    """
    return (scored["console_score"], scored["printable_ratio"],
            scored["has_crlf"], -scored["nulls"])


# -- everything below is orchestration support, not part of the brief's ------
# -- plan-supplied code -- but it is pure (no hardware, no I/O) and unit --
# -- tested the same way. -----------------------------------------------

DEFAULT_DWELL_SECONDS = 0.2
"""How long one candidate is listened to in ONE pass of the sweep.

Short on purpose. The scan used to make a single pass, dwelling ~1.5 s on
each candidate, which is correct for a line that keeps talking and wrong for
the target that found this: a Rockchip board emits a burst at boot and then
goes SILENT -- 12 s of listening after boot returned 0 bytes, measured. With
one long pass, whichever candidate's window happened to be open when the
burst arrived is the only rate that sees a byte at all, so it "wins" by being
lucky rather than by being right, and every other rate reports silence.

At 0.2 s the whole ladder is swept in well under half a second, so a burst
lasting a couple of seconds lands in SEVERAL windows per rate, and only the
correct rate accumulates readable text across them. This is the strategy that
found 1 500 000 on the first boot of the bench session, against a target the
one-pass scan could not detect at all.

The cost is per-window evidence: 0.2 s at 9600 baud is ~192 bytes, about two
lines. That is why a candidate's bytes ACCUMULATE across sweeps rather than
being replaced -- over a default budget the slowest candidate still collects
roughly a second of line, and `score_sample` reads the accumulation.
"""

DEFAULT_SCAN_BUDGET_SECONDS = 12.0
"""Total wall time one scan may spend sampling, across all sweeps.

Sweeping changes the SHAPE of the time a scan spends, not the amount: the old
single pass over the eight original candidates was 8 x 1.5 s = 12 s, and this
is the same 12 s spread over repeated short dwells. It is the budget, not the
candidate count, that bounds a scan now -- which is what lets the default
ladder grow to reach a new SoC family without the scan getting longer.

Also the bound on memory and on scoring cost, and the reason neither needs an
arbitrary cap: a scan reads for at most this long in total no matter how many
candidates are in the list, so the bytes it can accumulate are at most
`DEFAULT_SCAN_BUDGET_SECONDS * max(rate) / 10` across the whole scan -- about
3.6 MB at the top of the default ladder. `rates` is the only caller-supplied
input and it cannot raise that number.
"""

SAMPLE_PREVIEW_BYTES = 256
"""How much of each candidate's raw sample travels in the result.

Invariant 5 asks for "a short sample" alongside every candidate's scores, not
the whole capture -- this keeps the tool result compact for every candidate
rate at once rather than only the winner.
"""

WINNER_PRINTABLE_THRESHOLD = 0.85
"""How good a candidate's `printable_ratio` must be to be left live.

Unchanged, and now a GATE rather than the decision -- `console_score` is the
decision, and both must pass (see `looks_like_console`). Kept because it is
the one bar the bench's real numbers straddle cleanly: the genuine winner
measured 0.911 and the garbage from the same target at the wrong rate
measured 0.75. It is cheap, it is independent of the structural factors, and
a conjunction of two independent bars is strictly harder to fool than either.

What it must NOT be trusted to do alone is the thing it was previously asked
to do. 0.75 against 0.911 is a margin of 0.16, and simulating that
resampling shows misframed `printable_ratio` reaching 0.79 on some inter-byte
timings -- so this bar's own margin is a few percent wide and moves with the
target's transmit pattern. `console_score` is what carries the separation.
"""

WINNER_CONSOLE_THRESHOLD = 0.5
"""How high a candidate's `console_score` must be to be left live.

Placed from measurement, not from taste, and the band it sits in is wide in
both directions:

- genuine console output, ten varieties (boot log, 256-byte slice, LF-only
  console, hexdump, timestamp-only kernel log, busybox session, U-Boot
  banner): >= 0.744. This threshold is 1.5x below the worst of them.
- the same 1.5 Mbaud boot log misframed at every wrong candidate rate,
  74 physically simulated resamplings: <= 0.074. This threshold is 6.8x
  above the best of them.
- engineered noise at the bench's measured printable ratio WITH plausible
  `\r\n` inserted, i.e. an adversary aimed at exactly this test: 0.044.

Compare the quantity it replaces: on the bench's real numbers,
`printable_ratio` separated the genuine winner from the garbage by 0.911
against 0.75.
"""

WIRING_SUSPECT_CONSOLE_MAX = 0.25
"""Below this `console_score`, a candidate is not "a worse rate" -- it is
evidence that nothing on this line resembled console output at that speed.

Sits between the two populations measured above, nearer the garbage: every
simulated misframing scores under 0.074, so the hint fires reliably when a
scan really did see nothing but noise, while the band 0.25-0.5 is left for
the marginal candidate -- plausible text with real corruption, a rate near
the right one, worth retrying -- which must not draw a wiring accusation.
Calibrated against a corruption sweep of the genuine log: a link corrupting
half its bytes still scores 0.44 and is reported as marginal; one corrupting
60% drops to 0.21 and does draw the hint, which is the right call at that
level of damage.

It is deliberately NOT the complement of `WINNER_CONSOLE_THRESHOLD`:
"nothing scored well enough to win" is the ordinary outcome of a scan that
simply missed the right rate, and firing on every one of those would train a
caller to ignore the hint.

Which way to err was decided by the costs. A false positive costs an
operator one look at a ground wire. A false negative is the defect this
constant exists to fix: the scan confidently blames the rate, and the
operator spends the next hour on rates while the fault is the wire. The
ranked evidence is in the same response either way -- this only ever ADDS a
`hint`, it never replaces a verdict or changes a score.
"""


def looks_like_console(scored: dict) -> bool:
    """Both bars, and both must pass.

    A conjunction rather than a single number because the two bars fail in
    different directions: `printable_ratio` is fooled by noise that happens
    to land in the ASCII range, and `console_score` is fooled by anything
    that genuinely looks like structured text. Nothing measured on this
    bench clears both without being console output.
    """
    return (scored["console_score"] >= WINNER_CONSOLE_THRESHOLD
            and scored["printable_ratio"] >= WINNER_PRINTABLE_THRESHOLD)


def looks_like_console_bytes(data: bytes) -> bool:
    """`looks_like_console` over raw bytes -- the scan's early-exit predicate.

    Passed into `ConsoleSession.scan_baud` so the sweep can stop the moment
    one rate is unambiguous, without `session.py` having to know what a good
    sample looks like.
    """
    return looks_like_console(score_sample(data))


GROUND_CROSSOVER_HINT = (
    "Nothing on this line looked like console output at any candidate rate. "
    "Three causes produce that same evidence and the scan cannot tell them "
    "apart: (1) ground is not connected between the adapter and the target -- "
    "a floating ground produces framing errors that look exactly like a wrong "
    "baud rate; (2) TX/RX are swapped -- the adapter's TX must reach the "
    "target's RX and vice versa; (3) the target's rate is not in the "
    "candidate list -- pass `rates` explicitly if you know the part. Check "
    "the two wiring causes first: they cost one look each, and a rate list "
    "widened against a floating ground will fail at every rate too."
)
"""Invariant 6: name the physical causes, rather than just "try more rates".

Deliberately says nothing about WHICH symptom was observed -- the verdict and
its note carry that -- because the symptoms that warrant it are different and
only one of them is silence. A wrong crossover just produces silence, which
`no_data_at_any_rate` already reports honestly; a floating ground produces
framing errors that score exactly like a wrong baud rate, and that is the
case where the hint changes what an operator does.

THE THIRD CAUSE WAS ADDED FROM THE BENCH, and it was the one that actually
happened: the target's console ran at 1 500 000, `DEFAULT_RATES` stopped at
921 600, and this hint -- which at the time named only the two wiring faults
-- was attached to a rig whose wiring was perfectly correct. The earlier
version of this docstring argued that "the rate list was wrong" was the less
likely half of the pair because the defaults "already covers every common
console speed". That was the assumption the bench falsified, and the list
having been widened since does not make the assumption safe to keep: the next
part with an unusual rate produces exactly this again. Naming all three, and
ordering them by what costs the operator least to check, is what the hint can
honestly say.
"""


def all_scored_poorly(samples: dict[int, bytes]) -> bool:
    """True when EVERY candidate scored below `WIRING_SUSPECT_CONSOLE_MAX`.

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
        score_sample(data)["console_score"] < WIRING_SUSPECT_CONSOLE_MAX
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
array. 4 Mbaud is comfortably above every real UART console speed and
comfortably below the overflow boundary, so this rejects the class of input
that reaches it without rejecting anything a real board could plausibly use.

It must stay above `max(DEFAULT_RATES)` -- a ceiling that refused one of this
module's own defaults would refuse it silently, inside `sanitize_rates`,
where nothing would report which candidate went missing. No literal is
written here for the top of the ladder: it has already gone stale once (this
docstring named 921 600 while the list reached 3 000 000), and the invariant
is a relationship, not a number. `tests/unit/test_baud.py` asserts the
relationship.
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


def check_budget(num_rates: int, dwell_seconds: float, budget_seconds: float,
                 deadline_s: float) -> None:
    """Invariant 4: refuse a scan that cannot fit, don't discover it as a timeout.

    TWO ways it can fail to fit now that the scan sweeps, and they are
    different failures:

    1. The budget itself runs past the worker's request deadline. Unchanged
       in spirit from the single-pass check, but the budget no longer grows
       with the candidate count -- which is precisely what lets
       `DEFAULT_RATES` reach a new SoC family without the default list
       refusing itself.
    2. One full sweep of the candidate list does not fit inside the budget.
       This is the new one, and it is the honesty check: the scan guarantees
       every candidate is sampled at least once, so a list too long to sweep
       even once would either break that guarantee or overrun. Refusing here
       means the caller is told which of its rates would have gone unsampled
       BEFORE the port is touched, rather than reading a ranking that
       silently omitted them.
    """
    if budget_seconds > deadline_s:
        raise BaudScanError(
            f"a baud scan needs {budget_seconds:.1f}s, which exceeds this "
            f"worker's {deadline_s:.1f}s request deadline"
        )
    one_sweep = num_rates * dwell_seconds
    if one_sweep > budget_seconds:
        raise BaudScanError(
            f"sweeping {num_rates} candidate rate(s) at {dwell_seconds}s "
            f"each needs {one_sweep:.1f}s for a single pass, which does not "
            f"fit in the {budget_seconds:.1f}s scan budget; pass fewer rates "
            "so every candidate can be sampled at least once"
        )


def all_silent(samples: dict[int, bytes]) -> bool:
    """True when the line produced nothing at any candidate rate."""
    return not samples or all(len(data) == 0 for data in samples.values())


def pick_winner(samples: dict[int, bytes]) -> int | None:
    """The rate to leave the port at, or `None` to restore the original.

    Ranks by `sort_key` (`console_score` first) and only accepts the best
    candidate if it also passes `looks_like_console` -- BOTH bars, not merely
    the best of what turned up. See `WINNER_CONSOLE_THRESHOLD` for the
    measured populations either side, and `WINNER_PRINTABLE_THRESHOLD` for
    why the older bar is kept alongside it rather than replaced.
    """
    best_rate: int | None = None
    best_key: tuple | None = None
    for rate, data in samples.items():
        if not data:
            continue
        key = sort_key(score_sample(data))
        if best_key is None or key > best_key:
            best_key, best_rate = key, rate
    if best_rate is None:
        return None
    if not looks_like_console(score_sample(samples[best_rate])):
        return None
    return best_rate


def rank_candidates(samples: dict[int, bytes],
                    listen_seconds: dict[int, float] | None = None) -> list[dict]:
    """Every candidate, scored, best-first. Invariant 5: ranked evidence, never a verdict.

    Each entry carries the raw (untruncated only up to `SAMPLE_PREVIEW_BYTES`)
    sample bytes under `"sample"` -- callers over the wire (`tools.py`) must
    base64-encode it themselves, the same way every other target-byte field in
    this worker travels, before it reaches an untrusted context.

    `listen_seconds`, when given, adds how long the scan actually listened at
    each rate. That is what keeps a sweep with an early exit honest: every
    candidate is sampled, but they are not all sampled for the same length of
    time, and a caller comparing two candidates' evidence needs to see which
    of them got half as much of it.
    """
    candidates = []
    for rate, data in samples.items():
        scored = score_sample(data)
        entry = {
            "rate": rate,
            "bytes_captured": len(data),
            "sample": data[:SAMPLE_PREVIEW_BYTES],
            "sample_truncated": len(data) > SAMPLE_PREVIEW_BYTES,
            **scored,
        }
        if listen_seconds is not None:
            entry["listen_seconds"] = round(listen_seconds.get(rate, 0.0), 3)
        candidates.append(entry)
    candidates.sort(key=lambda c: sort_key(c), reverse=True)
    return candidates
