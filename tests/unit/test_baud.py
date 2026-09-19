# tests/unit/test_baud.py
from __future__ import annotations

import collections
import random

import pytest

import _uart
from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_DWELL_SECONDS,
                                    DEFAULT_RATES,
                                    DEFAULT_SCAN_BUDGET_SECONDS,
                                    GROUND_CROSSOVER_HINT, MAX_BAUD_RATE,
                                    WINNER_CONSOLE_THRESHOLD,
                                    WINNER_PRINTABLE_THRESHOLD,
                                    SPACE_RATIO_REFERENCE,
                                    WIRING_SUSPECT_CONSOLE_MAX,
                                    all_scored_poorly, all_silent,
                                    check_budget, looks_like_console,
                                    pick_winner, rank_candidates,
                                    sanitize_rates, score_sample)
from pare_hardware_mcp.config import Config

def _bit_runs(byte: int) -> int:
    """How many unbroken runs of like bits the byte is, in TIME order.

    UART sends LSB first, so that is the order the receiver samples in. A
    byte that is one or two runs is a solid block -- what resampling a
    waveform at the wrong clock produces. 0x20 is three runs: a lone 1
    between two blocks of 0s.
    """
    seq = [(byte >> k) & 1 for k in range(8)]
    return 1 + sum(1 for i in range(7) if seq[i] != seq[i + 1])


GAPS = (0, 1, 2, 3, 5, 8)
"""Transmitter inter-byte idle times swept, in bit periods. See `_uart.py`:
the gap is a free parameter because the real one is unknowable, and sweeping
it is what shows `printable_ratio` to be unstable."""

MISFRAMED = [
    (gap, tx, rx, _uart.misframed(rx, gap, tx_baud=tx))
    for gap in GAPS
    for tx in DEFAULT_RATES
    for rx in DEFAULT_RATES
    if rx < tx
]
MISFRAMED = [t for t in MISFRAMED if t[3]]
"""Every ordered rate pair in the default ladder where the RECEIVER IS THE
SLOWER of the two, across `GAPS` transmitter timings.

This used to be sourced from a single transmit rate (1 500 000, the bench's
target), which meant the suite never saw a near-neighbour ratio -- the
2:1 and 5:4 cases where a wrong rate resembles the right one most closely,
and where the worst-scoring garbage in fact lives. Parameterising over
`tx_baud` is what brings those under test.

WHY ONLY `rx < tx`. The two halves were timed separately by driving
`_uart.misframed` over each one directly; the cost of simulating a pair is
proportional to `rx / tx`, because the receiver's step size shrinks with its
own bit period while the waveform's length is set by the transmitter's:

- `rx < tx`, the half kept here: 330 samples, ~1.6 s to build, and the worst
  `console_score` in it is 0.100 -- a fifth of `WINNER_CONSOLE_THRESHOLD`,
  and it occurs at tx=3 000 000 / rx=57 600, a pair the old single-tx fixture
  could not produce.
- `rx > tx`, the half left out: another 330 samples costing **410 s** to
  build -- 265x the runtime of the half that is kept -- for a population
  whose worst `console_score` is **0.0020**. That number does not move when
  the payload is shortened or the inter-byte gap is changed (measured at a
  0.2 s and a 0.05 s dwell-realistic payload: still 0.0020, and still 27 s
  at the cheaper one). A receiver clocked faster than the transmitter
  oversamples every bit, so it yields long runs of 0x00 and 0xff, which the
  scorer already rejects by a factor of 250. It is the easy half, and it is
  the expensive half.

`OVERSAMPLED` below keeps the fast-receiver direction present in the suite
at the one place it is cheap, so the asymmetry is documented rather than
silently absent. To re-measure either half, change the `rx < tx` predicate
above and time the collection; nothing else in this file depends on it.
"""

OVERSAMPLED = [
    (gap, _uart.ROCKCHIP_CONSOLE_BAUD, 3000000,
     _uart.misframed(3000000, gap, tx_baud=_uart.ROCKCHIP_CONSOLE_BAUD))
    for gap in GAPS
]
"""The fast-receiver direction, at the only tx rate where it is cheap.

The bench's own target (1 500 000) heard at the top of the ladder. ~0.3 s to
build, against 410 s for the whole `rx > tx` half -- see `MISFRAMED`. Kept so
that "a receiver clocked faster than the transmitter" is a case the suite
actually evaluates rather than one a docstring asserts about.
"""

GENUINE = {
    "boot log": _uart.BOOT_LOG,
    "LF-only console": _uart.BOOT_LOG.replace(b"\r\n", b"\n"),
    "256-byte slice": _uart.BOOT_LOG[:256],
    "U-Boot banner": b"U-Boot 2021.01\r\nHit any key to stop autoboot\r\n" * 4,
    "hexdump": b"\r\n".join(
        b"%08x  %s |................|"
        % (i * 16, b" ".join(b"%02x" % ((i * 7 + j) & 0xFF) for j in range(16)))
        for i in range(60)),
    "timestamp-only kernel log": b"\r\n".join(
        b"[%10.6f] %d %d %d" % (i / 1000, i, i * 3, i * 7) for i in range(120)),
}
"""The varieties of GENUINE console output the scorer must not reject.

Collected here because `baud.py`'s docstrings used to quote a COUNT of them
("ten varieties") and a floor on their scores, and both had drifted away from
what the suite actually contained -- there were never ten, and nothing
asserted either figure, so nothing caught the drift. The docstrings now point
at this dict and at the tests below, which assert the RELATIONSHIPS the
figures were standing in for. Add a variety here and the bars are re-checked
against it automatically.

Deliberately spans the awkward cases: output with no words in it at all (the
hexdump and the timestamp-only log have zero runs of three letters), an
LF-only console, and a sample short enough that `line_structure`'s
small-sample behaviour is in play.
"""


def test_readable_text_scores_far_above_high_bit_noise():
    good = score_sample(b"U-Boot 2021.01\r\nHit any key to stop autoboot\r\n")
    bad = score_sample(bytes(range(128, 256)) * 2)
    assert good["printable_ratio"] > 0.9
    assert bad["printable_ratio"] < 0.2
    assert good["printable_ratio"] > bad["printable_ratio"]


def test_crlf_is_reported_because_console_output_is_line_oriented():
    assert score_sample(b"line one\r\nline two\r\n")["has_crlf"] is True
    assert score_sample(b"no newlines here")["has_crlf"] is False


def test_an_empty_sample_scores_zero_rather_than_dividing_by_zero():
    s = score_sample(b"")
    assert s["printable_ratio"] == 0.0


def test_nulls_are_counted_since_a_wrong_rate_produces_them():
    assert score_sample(b"\x00\x00ab")["nulls"] == 2


def test_zero_is_not_a_candidate_rate():
    # termios treats speed 0 as "drop the modem control lines", which asserts
    # the DTR reset this scan exists to avoid.
    assert 0 not in DEFAULT_RATES
    assert all(r > 0 for r in DEFAULT_RATES)


def test_the_default_rates_are_ordered_and_cover_the_common_console_speeds():
    assert list(DEFAULT_RATES) == sorted(DEFAULT_RATES)
    for r in (9600, 115200):
        assert r in DEFAULT_RATES


def test_the_default_ladder_reaches_the_high_speed_soc_console_rates():
    """The bench defect: 1500000 was UNREACHABLE by default.

    A Rockchip target's console runs at 1500000 and the ladder stopped at
    921600, so no default scan could find it -- and because every candidate
    then scored like noise, the run ended by blaming wiring that was
    correct. Would catch the pre-fix tuple, and any later trim of the top of
    the ladder back below a whole SoC family.
    """
    for rate in (1152000, 1500000, 3000000):
        assert rate in DEFAULT_RATES, (
            f"{rate} is a standard console rate on a common SoC family and "
            "cannot be reached by a default scan")


def test_max_baud_rate_admits_every_default_candidate():
    """A ceiling under one of this module's own defaults would drop it
    silently inside `sanitize_rates`, with nothing reporting which candidate
    went missing.

    Asserted as a RELATIONSHIP, not against 4_000_000 or against the top of
    the ladder: both have already moved once, and pinning either literal
    would break on the next legitimate change while still not checking the
    thing that matters. Would catch a new default above the ceiling, and a
    ceiling lowered below an existing default.
    """
    assert max(DEFAULT_RATES) <= MAX_BAUD_RATE
    assert sanitize_rates(list(DEFAULT_RATES)) == DEFAULT_RATES


def test_the_default_ladder_does_not_refuse_its_own_scan():
    """A default that the budget check rejects would be worse than the bug.

    Two ways to break it, and this catches both: growing the ladder until one
    sweep no longer fits the budget, and raising the budget past the worker's
    own request deadline. Uses `Config()`'s real defaults rather than numbers
    typed here, so it tracks the worker as SHIPPED -- the budget is now an
    operator-overridable setting (PARE_HW_SCAN_BUDGET_S) whose default it
    takes from this module, and it is the shipped pair that must not refuse
    itself.
    """
    cfg = Config()
    check_budget(len(DEFAULT_RATES), DEFAULT_DWELL_SECONDS,
                 cfg.scan_budget_s, cfg.request_deadline_s)
    assert cfg.scan_budget_s == DEFAULT_SCAN_BUDGET_SECONDS


# --------------------------------------------------------------------------
# sanitize_rates -- invariant 2: 0 is filtered before the port is touched.
# --------------------------------------------------------------------------

def test_sanitize_rates_defaults_to_default_rates_when_none_given():
    assert sanitize_rates(None) == DEFAULT_RATES


def test_sanitize_rates_drops_zero_and_negative_and_non_integers():
    assert sanitize_rates([0, 9600, -1, 115200, True, 3.5]) == (9600, 115200)


def test_sanitize_rates_dedupes_preserving_order():
    assert sanitize_rates([115200, 9600, 115200]) == (115200, 9600)


def test_sanitize_rates_refuses_when_nothing_usable_remains():
    with pytest.raises(BaudScanError) as e:
        sanitize_rates([0, -5, True])
    assert "0" in str(e.value) or "no usable" in str(e.value)


def test_sanitize_rates_drops_a_rate_above_the_ceiling():
    # Reproduces the review's finding: pyserial's custom-baud path stores the
    # rate in a C int (array('i')), which overflows for anything >= 2**31,
    # and a caller-supplied `rates` array is otherwise unconstrained.
    assert sanitize_rates([9600, 2_147_483_648, 115200]) == (9600, 115200)
    assert sanitize_rates([MAX_BAUD_RATE, MAX_BAUD_RATE + 1]) == (MAX_BAUD_RATE,)


def test_sanitize_rates_refuses_when_only_over_ceiling_values_remain():
    with pytest.raises(BaudScanError):
        sanitize_rates([2_147_483_648, MAX_BAUD_RATE + 1])


# --------------------------------------------------------------------------
# check_budget -- invariant 4: bounded and derived, checked against the
# worker's deadline rather than discovered as a timeout.
# --------------------------------------------------------------------------

def test_check_budget_accepts_a_list_within_the_deadline():
    check_budget(num_rates=4, dwell_seconds=0.2, budget_seconds=12.0,
                 deadline_s=60.0)  # no raise


def test_check_budget_refuses_a_budget_that_would_exceed_the_deadline():
    with pytest.raises(BaudScanError) as e:
        check_budget(num_rates=4, dwell_seconds=0.2, budget_seconds=90.0,
                     deadline_s=60.0)
    message = str(e.value)
    assert "90.0" in message and "60.0" in message


def test_check_budget_refuses_a_list_too_long_to_sweep_even_once():
    """The condition that replaces "the list costs more than the deadline".

    The scan guarantees every candidate is sampled at least once before an
    early exit can be taken; a list too long to sweep once would either break
    that guarantee or overrun the budget. Would catch the budget check
    reduced to the deadline comparison alone -- 100 rates at 0.2s is 20s of
    sweep, which fits a 60s deadline perfectly well and still cannot be
    swept inside a 12s budget.
    """
    with pytest.raises(BaudScanError) as e:
        check_budget(num_rates=100, dwell_seconds=0.2, budget_seconds=12.0,
                     deadline_s=60.0)
    assert "20.0" in str(e.value) and "12.0" in str(e.value)


def test_check_budget_does_not_grow_its_refusal_with_the_candidate_count():
    """A longer ladder must not cost more wall time -- that is the whole
    point of sweeping a fixed budget, and it is what lets `DEFAULT_RATES`
    reach a new SoC family without the default refusing itself. Would catch
    the deadline arm rebuilt as `num_rates * dwell`.
    """
    for count in (2, 8, 30):
        check_budget(count, 0.2, 12.0, deadline_s=12.0)  # no raise


# --------------------------------------------------------------------------
# pick_winner -- ranked evidence, never a bare verdict; an absolute bar, not
# merely best-of-a-bad-lot.
# --------------------------------------------------------------------------

GOOD = b"U-Boot 2021.01\r\nHit any key to stop autoboot\r\n" * 4
BAD = bytes(range(128, 256)) * 4


def test_pick_winner_picks_the_clearly_readable_candidate():
    assert pick_winner({9600: BAD, 115200: GOOD, 230400: BAD}) == 115200


def test_pick_winner_returns_none_when_every_candidate_is_garbage():
    assert pick_winner({9600: BAD, 115200: BAD}) is None


def test_pick_winner_returns_none_for_no_data_at_all():
    assert pick_winner({9600: b"", 115200: b""}) is None
    assert pick_winner({}) is None


def test_pick_winner_does_not_crown_a_merely_best_of_a_bad_lot_candidate():
    # Both candidates are noise; one happens to score marginally higher by
    # chance. Comparison alone must not be enough to win.
    barely_less_bad = bytes([65]) + bytes(range(128, 250))
    worse = bytes(range(128, 256))
    assert pick_winner({9600: worse, 115200: barely_less_bad}) is None


# --------------------------------------------------------------------------
# rank_candidates and all_silent -- invariants 5 and 6.
# --------------------------------------------------------------------------

def test_rank_candidates_ranks_best_first_and_carries_every_candidate():
    ranked = rank_candidates({9600: BAD, 115200: GOOD})
    assert [c["rate"] for c in ranked] == [115200, 9600]
    assert all({"rate", "sample", "printable_ratio", "has_crlf", "nulls",
               "bytes_captured", "sample_truncated"} <= c.keys()
              for c in ranked)


def test_rank_candidates_truncates_the_sample_but_reports_the_full_length():
    from pare_hardware_mcp.baud import SAMPLE_PREVIEW_BYTES
    long_sample = b"A" * (SAMPLE_PREVIEW_BYTES + 100)
    ranked = rank_candidates({115200: long_sample})
    assert len(ranked[0]["sample"]) == SAMPLE_PREVIEW_BYTES
    assert ranked[0]["bytes_captured"] == len(long_sample)
    assert ranked[0]["sample_truncated"] is True


def test_all_silent_is_true_only_when_every_candidate_captured_nothing():
    assert all_silent({9600: b"", 115200: b""}) is True
    assert all_silent({}) is True
    assert all_silent({9600: b"", 115200: b"x"}) is False


def test_ground_crossover_hint_names_ground_and_crossover():
    assert "ground" in GROUND_CROSSOVER_HINT.lower()
    assert "tx" in GROUND_CROSSOVER_HINT.lower() and "rx" in GROUND_CROSSOVER_HINT.lower()


# --------------------------------------------------------------------------
# all_scored_poorly -- invariant 6's REAL condition. A floating ground
# produces framing errors that score exactly like a wrong baud rate, so
# "every candidate scored poorly" is the case where the hint changes what an
# operator does; silence is the benign half that no_data_at_any_rate already
# reports honestly.
# --------------------------------------------------------------------------

def test_high_bit_noise_at_every_rate_scores_poorly():
    # A floating ground's signature. Would catch the predicate written
    # against `has_crlf` or `nulls` rather than `printable_ratio`.
    noise = bytes(range(128, 256))
    assert all_scored_poorly({9600: noise, 115200: noise}) is True


def test_one_clean_candidate_is_enough_to_clear_the_suspicion():
    # Would catch `any(...)` written where `all(...)` belongs.
    assert all_scored_poorly({9600: bytes(range(128, 256)), 115200: GOOD}) is False


def test_the_suspicion_threshold_is_not_the_winner_threshold():
    """An ordinary missed-rate scan must not read as a wiring fault.

    Pinned as a RELATIONSHIP between the two constants rather than against
    either value, so moving a threshold for a real reason does not break
    this, but collapsing the two into one does. Would catch
    WIRING_SUSPECT_CONSOLE_MAX being set to WINNER_CONSOLE_THRESHOLD, which
    would fire the hint on every scan that simply missed the rate.
    """
    assert WIRING_SUSPECT_CONSOLE_MAX < WINNER_CONSOLE_THRESHOLD
    # A sample in the band between them: too poor to win, too good to accuse
    # the wiring.
    marginal = b"U-Boot 2021.01 \r\n" * 6 + bytes(range(128, 160))
    scored = score_sample(marginal)
    assert WIRING_SUSPECT_CONSOLE_MAX < scored["console_score"]
    assert not looks_like_console(scored)
    assert pick_winner({9600: marginal}) is None
    assert all_scored_poorly({9600: marginal}) is False


def test_an_empty_sample_set_is_not_annexed_by_the_poor_scoring_check():
    """`all(...)` over an empty dict is True, which would let this predicate
    claim the all-rates-rejected and nothing-sampled cases -- both of which
    have their own verdict. Would catch the `if not samples` guard being
    dropped."""
    assert all_scored_poorly({}) is False


def test_a_silent_candidate_set_still_scores_poorly():
    """Empty samples score 0.0, so silence is a subset of "everything poor".
    The two verdicts are separated in tools.py, by checking all_silent first
    -- this pins that the predicate itself does not exclude it, so a future
    reorder cannot silently drop the hint from a silent line."""
    assert all_scored_poorly({9600: b"", 115200: b""}) is True


# --------------------------------------------------------------------------
# The discriminator, against DERIVED garbage rather than invented garbage.
#
# `tests/unit/_uart.py` transmits a boot log at 1500000 and receives it at
# every wrong candidate rate, which is the fault the bench actually hit. The
# assertions below are about the MARGIN between the two populations, because
# a margin is the property that matters and a single threshold comparison
# would pass just as well against a discriminator with no margin at all.
# --------------------------------------------------------------------------

def test_the_simulator_reproduces_the_benchs_printable_ratio():
    """The calibration that makes every other test in this section mean
    something.

    The bench measured `printable_ratio` 0.75 for real garbage at the wrong
    rate. If the simulator produced, say, 0.05, it would be modelling
    something other than the observed fault and the margins below would be
    fiction. Asserted as "the measurement lies inside the simulated range"
    rather than "the simulator produces 0.75", because the RANGE is the
    finding: the ratio is not stable across transmit timings.
    """
    ratios = [score_sample(d)["printable_ratio"] for *_, d in MISFRAMED]
    assert min(ratios) < 0.75 < max(ratios), (min(ratios), max(ratios))


def test_printable_ratio_alone_cannot_separate_garbage_from_console_text():
    """The defect, stated as a test: the OLD discriminator fails here.

    Some misframed samples score above 0.5 -- a bare printable-ratio bar the
    old wiring check used, which is gone along with the constant that named
    it (`WIRING_SUSPECT_PRINTABLE_MAX`; `WIRING_SUSPECT_CONSOLE_MAX` replaced
    it on the console-score axis, and is not the same number or the same
    quantity). Those samples would have been read as "a worse rate, not a
    wiring problem", and some come within a few points of
    `WINNER_PRINTABLE_THRESHOLD`. The 0.5 below is therefore a HISTORICAL
    literal, deliberately not resolved to a live constant: it records what
    the deleted code did, and there is nothing in the module for it to track.

    This test does not assert the fix -- it pins the reason for it, so a
    future change that quietly reverts to printable-only scoring has
    something to fail against.
    """
    ratios = [score_sample(d)["printable_ratio"] for *_, d in MISFRAMED]
    assert max(ratios) > 0.5, (
        "if no simulated misframing scores above 0.5 the premise of this "
        "whole section is wrong")
    assert max(ratios) > WINNER_PRINTABLE_THRESHOLD - 0.1


def test_every_misframed_sample_scores_far_below_genuine_console_output():
    """The margin, over all of the derived garbage at once.

    Would catch: `console_score` reduced to `printable_ratio`, the space
    factor dropped, the line factor dropped, or any of the three replaced by
    a constant -- each of those collapses this margin. A single-sample
    version of this test would not: the populations overlap heavily on any
    one factor.
    """
    genuine = score_sample(_uart.BOOT_LOG)["console_score"]
    worst = max(score_sample(d)["console_score"] for *_, d in MISFRAMED)
    assert genuine > 0.74, genuine
    assert worst < WIRING_SUSPECT_CONSOLE_MAX, worst
    assert genuine > 5 * worst, (genuine, worst)


def test_every_genuine_variety_clears_the_winner_bar_with_margin():
    """The floor `baud.py` used to quote as a literal ("ten varieties ...
    >= 0.744"), asserted as a RELATIONSHIP instead.

    There were never ten varieties and nothing asserted the floor, so the
    figure drifted unnoticed. What the figure was standing in for is that
    every kind of genuine console output clears `WINNER_CONSOLE_THRESHOLD`
    with room to spare -- which is checked here against `GENUINE`, so adding
    a variety re-checks it and no number has to be maintained by hand.
    """
    for name, sample in GENUINE.items():
        scored = score_sample(sample)
        assert looks_like_console(scored), (name, scored)
        assert scored["console_score"] > 1.4 * WINNER_CONSOLE_THRESHOLD, (
            name, scored["console_score"])


def test_every_misframed_sample_scores_a_fraction_of_the_winner_bar():
    """The other half of the separation, likewise as a relationship.

    `baud.py` used to quote "<= 0.074" for a fixture of 60 samples that its
    prose called 74. The property is that the WHOLE misframed population sits
    far under the bar -- now over every ordered rate pair with a slower
    receiver, including the near-neighbour ratios the old single-tx fixture
    could not reach.
    """
    worst = max(score_sample(d)["console_score"] for *_, d in MISFRAMED)
    assert worst < WINNER_CONSOLE_THRESHOLD / 3, worst
    best_genuine_floor = min(
        score_sample(v)["console_score"] for v in GENUINE.values())
    assert best_genuine_floor > 5 * worst, (best_genuine_floor, worst)


def test_misframed_bytes_are_concentrated_rather_than_uniform():
    """The MECHANISM correction: they do not spread out evenly at all.

    `SPACE_RATIO_REFERENCE` used to argue that resampling "spreads the result
    across the reachable byte values roughly evenly, so no value -- 0x20
    included -- gets more than a few percent". That is false, and this test
    is what makes it stay false-and-known: the distribution is heavily
    concentrated, and the real reason 0x20 stays rare is the SHAPE of the
    values it concentrates on (see the next test), not their spread.

    A maintainer deciding whether `space_factor` is redundant has to reason
    from the true mechanism; reasoning from "roughly uniform" gets the wrong
    answer.
    """
    shares, distinct = [], []
    for *_, data in MISFRAMED:
        counts = collections.Counter(data)
        shares.append(counts.most_common(1)[0][1] / len(data))
        distinct.append(len(counts))
    assert max(shares) > 0.5, max(shares)
    assert min(distinct) < 16, min(distinct)


def test_misframing_suppresses_the_bit_pattern_that_makes_0x20():
    """Why `space_ratio` separates, now that "roughly uniform" is gone.

    Resampling a bit stream at the wrong clock turns each sampled byte into a
    few long RUNS -- the sample window straddles whole tx bit times, so it
    picks up blocks of like bits. 0x20 is the opposite shape: a single
    isolated set bit in a field of zeros. So the process that produces
    misframed bytes structurally under-produces exactly the value console
    text is richest in.

    Two independent measurements of that, both against the committed
    simulator, and both stated as ratios between the two populations so
    neither can rot into a bare literal.
    """
    misframed_bytes = b"".join(d for *_, d in MISFRAMED)
    genuine_bytes = _uart.BOOT_LOG

    def share(data, predicate):
        return sum(1 for b in data if predicate(b)) / len(data)

    # A byte that is a single unbroken block of 0s or 1s: what resampling
    # makes, and what ASCII text never contains.
    assert share(genuine_bytes, lambda b: _bit_runs(b) <= 2) == 0.0
    assert share(misframed_bytes, lambda b: _bit_runs(b) <= 2) > 0.05

    # The six values shaped like 0x20 -- one set bit, isolated. Console text
    # is made of them; misframed bytes are not.
    isolated = {b for b in range(256)
                if bin(b).count("1") == 1 and _bit_runs(b) == 3}
    assert 0x20 in isolated
    genuine_share = share(genuine_bytes, lambda b: b in isolated)
    misframed_share = share(misframed_bytes, lambda b: b in isolated)
    assert genuine_share > 5 * misframed_share, (genuine_share,
                                                 misframed_share)

    # The high-bit bias the same process produces, for completeness: stop and
    # idle bits are 1 and ASCII's top bit is always 0.
    assert share(genuine_bytes, lambda b: b & 0x80) == 0.0
    assert share(misframed_bytes, lambda b: b & 0x80) > 0.5


def test_no_misframed_sample_reaches_the_space_ratio_reference():
    """`space_ratio` is the factor a wrong rate cannot fake, pinned directly.

    Every genuine variety reaches `SPACE_RATIO_REFERENCE` and so is not
    penalised at all; no misframed sample of a workable length does.

    The length floor is honest rather than convenient: one sample in the
    fixture is 9 bytes long (tx=3 000 000 heard at 9 600) and a single space
    in it reads as 0.111, over the reference. That is counting noise at n=9,
    not evidence about the mechanism -- and it cannot win regardless, since
    its `console_score` is still a fifth of the bar. Above 16 bytes the
    highest misframed `space_ratio` in the whole population is 0.084.
    """
    for name, sample in GENUINE.items():
        assert score_sample(sample)["space_ratio"] >= SPACE_RATIO_REFERENCE, name
    workable = [(tx, rx, d) for _, tx, rx, d in MISFRAMED if len(d) >= 16]
    # Relational, so the length filter can never quietly empty the list and
    # make every assertion below vacuous.
    assert len(workable) > 0.9 * len(MISFRAMED), (len(workable),
                                                  len(MISFRAMED))
    for tx, rx, data in workable:
        assert score_sample(data)["space_ratio"] < SPACE_RATIO_REFERENCE, (
            tx, rx, len(data), score_sample(data)["space_ratio"])


def test_short_windows_of_misframed_bytes_clear_neither_gate():
    """`line_structure`'s known weak spot, attacked deliberately.

    Below `MAX_PLAUSIBLE_LINE_LENGTH` bytes `expected` floors at 1.0, so a
    single terminator anywhere in a short window scores `line_structure` 1.0.
    A scan accumulates bytes across sweeps, but early in a scan -- or at a
    slow rate -- a candidate's sample really is this short, so the question
    is whether some window of misframed bytes can get through on that.

    It cannot, and the conjunction is what stops it: short windows do push
    `console_score` up (the worst reaches about 0.39, well over
    `WIRING_SUSPECT_CONSOLE_MAX`), but none of them clears the winner bar and
    the printable gate together. Would catch `looks_like_console` weakened to
    either bar alone, which is the change this window population is sized to
    punish.
    """
    checked = 0
    for _, tx, rx, data in MISFRAMED:
        for width in (16, 32, 64, 128, 256):
            for i in range(0, len(data) - width + 1, max(1, width // 2)):
                scored = score_sample(data[i:i + width])
                checked += 1
                assert not looks_like_console(scored), (tx, rx, i, width,
                                                        scored)
    # Relational non-vacuity guard: every sample must have contributed
    # windows, so a fixture change cannot silently reduce this to nothing.
    assert checked > 10 * len(MISFRAMED), (checked, len(MISFRAMED))


def test_a_receiver_faster_than_the_transmitter_produces_nothing_scorable():
    """The other direction, which `MISFRAMED` deliberately does not sweep.

    See `OVERSAMPLED`: the whole `rx > tx` half costs 410 s to build for a
    population whose worst `console_score` is 0.002, because oversampling
    every bit yields long runs of 0x00 and 0xff. This keeps the direction
    under test at the one pair where it is cheap, so the claim in
    `MISFRAMED`'s docstring is checked rather than merely asserted.
    """
    assert OVERSAMPLED, "the fast-receiver fixture must not be empty"
    for gap, tx, rx, data in OVERSAMPLED:
        scored = score_sample(data)
        assert data, (gap, tx, rx)
        assert scored["console_score"] == 0.0, (gap, tx, rx, scored)
        assert not looks_like_console(scored), (gap, tx, rx, scored)


def test_no_misframed_sample_is_ever_crowned_a_winner():
    """The failure that would leave the port applied at a wrong rate.

    Every misframed sample, presented alone so there is nothing better to
    lose to. Would catch a winner rule that ranks by comparison instead of
    against an absolute bar, and any threshold set below the top of the
    garbage population.
    """
    for gap, tx, rx, data in MISFRAMED:
        assert pick_winner({rx: data}) is None, (gap, tx, rx,
                                                 score_sample(data))


def test_the_genuine_rate_wins_against_the_whole_misframed_field():
    """The bench's actual scan, end to end through the scorer: the real rate
    against every wrong rate in the ladder, all sampled at once."""
    samples = {rx: data for gap, tx, rx, data in MISFRAMED
               if rx != 1500000 and tx == _uart.ROCKCHIP_CONSOLE_BAUD}
    samples[1500000] = _uart.BOOT_LOG
    assert pick_winner(samples) == 1500000
    assert rank_candidates(samples)[0]["rate"] == 1500000


def test_noise_that_looks_printable_and_has_crlf_still_does_not_win():
    """The adversary aimed at exactly this discriminator, and the test that
    a threshold move alone would fail.

    Bytes chosen to clear `WINNER_PRINTABLE_THRESHOLD` outright, with `\r\n`
    inserted at plausible line lengths so the line factor cannot reject it
    either. Under the pre-fix rule -- printable_ratio >= 0.85 and nothing
    else -- this WINS and the port is left at a rate that produced noise.
    Would catch: the fix implemented as a raised printable threshold, the
    space factor omitted, or `looks_like_console` weakened to `or`.
    """
    rng = random.Random(20260913)
    noise = bytearray()
    while len(noise) < 4000:
        for _ in range(rng.randrange(40, 70)):
            # 0x21-0x7e: printable, and deliberately never 0x20.
            noise.append(rng.randrange(0x21, 0x7f))
        noise += b"\r\n"
    scored = score_sample(bytes(noise))
    assert scored["printable_ratio"] >= WINNER_PRINTABLE_THRESHOLD, scored
    assert scored["has_crlf"] is True
    assert scored["line_structure"] == 1.0
    assert not looks_like_console(scored), scored
    assert pick_winner({115200: bytes(noise)}) is None


def test_an_lf_only_console_is_still_recognised():
    """A real configuration, and the false negative a CRLF-only rule creates.

    `\r\n` is the strong signal and `\n` is the fallback; a console whose
    driver does not translate must still win. Would catch the line factor
    written against `\r\n` alone.
    """
    lf_only = _uart.BOOT_LOG.replace(b"\r\n", b"\n")
    assert b"\r" not in lf_only
    assert looks_like_console(score_sample(lf_only))
    assert pick_winner({1500000: lf_only}) == 1500000


def test_console_output_without_words_is_still_recognised():
    """Hexdumps and timestamp-only kernel lines are ordinary console output
    and contain no runs of letters at all.

    Would catch a structural factor built on word or letter runs, which
    scores both of these zero -- a false negative on working hardware, which
    is how a discriminator gets quietly disabled by the next person who hits
    it.
    """
    hexdump = b"\r\n".join(
        b"%08x  %s |................|"
        % (i * 16, b" ".join(b"%02x" % ((i * 7 + j) & 0xFF) for j in range(16)))
        for i in range(60))
    timestamps = b"\r\n".join(
        b"[%10.6f] %d %d %d" % (i / 1000, i, i * 3, i * 7) for i in range(120))
    for sample in (hexdump, timestamps):
        assert looks_like_console(score_sample(sample)), score_sample(sample)


def test_a_silent_line_and_a_floating_ground_both_score_zero():
    """Unchanged behaviour, pinned because the scorer was rewritten.

    Silence and high-bit noise both score 0.0 and neither can win. tools.py
    separates them by checking `all_silent` FIRST -- the scorer itself must
    not start distinguishing them, since it has no evidence that would.
    """
    assert score_sample(b"")["console_score"] == 0.0
    assert score_sample(bytes(range(128, 256)) * 8)["console_score"] == 0.0
    assert pick_winner({9600: b"", 115200: bytes(range(128, 256))}) is None


def test_console_output_with_no_line_terminator_cannot_win():
    """A DELIBERATE false negative, pinned so it is a decision rather than a
    surprise.

    A bootloader echoing `=> ` and nothing else is genuine console output
    with no line structure, and it scores 0.0. The scan then restores the
    original rate and returns ranked evidence, which is the fail-safe
    direction -- no rate is applied on the strength of a sample with no
    structure in it. Documented in `score_sample`'s docstring; if this test
    is ever deleted, that paragraph must go with it.
    """
    prompt = b"=> " * 200
    assert score_sample(prompt)["printable_ratio"] == 1.0
    assert score_sample(prompt)["console_score"] == 0.0
    assert pick_winner({115200: prompt}) is None


def test_ranking_leads_on_the_console_score_not_on_printability():
    """Ranked evidence is what a caller reads when nothing won, so the order
    has to reflect the discriminator and not the quantity it replaced.

    The garbage here is MORE printable than the genuine sample, which is
    exactly the bench's shape at a smaller margin. Would catch the sort key
    left on `printable_ratio`.
    """
    genuine = _uart.BOOT_LOG.replace(b"e", b"\xe9", 40)  # slightly corrupted
    garbage = bytes(random.Random(7).randrange(0x21, 0x7f) for _ in range(3000))
    assert (score_sample(garbage)["printable_ratio"]
            > score_sample(genuine)["printable_ratio"])
    ranked = rank_candidates({115200: garbage, 1500000: genuine})
    assert ranked[0]["rate"] == 1500000


def test_rank_candidates_reports_how_long_each_rate_was_listened_to():
    """A sweep with an early exit does not give every candidate equal time,
    and a caller comparing two candidates' evidence needs to see that.
    Would catch `listen_seconds` dropped on the floor between session.py and
    the response.
    """
    ranked = rank_candidates({9600: BAD, 115200: GOOD},
                             {9600: 0.4, 115200: 1.2000001})
    by_rate = {c["rate"]: c for c in ranked}
    assert by_rate[9600]["listen_seconds"] == 0.4
    assert by_rate[115200]["listen_seconds"] == 1.2
    # Omitted rather than faked when the scan did not report it.
    assert "listen_seconds" not in rank_candidates({9600: BAD})[0]


def test_the_wiring_hint_also_names_a_rate_outside_the_candidate_list():
    """The bench's own experience: correct wiring, hint fired anyway.

    The hint named only ground and TX/RX, so a rig whose only fault was a
    console rate above the ladder was told to inspect wire that was fine.
    Would catch the fourth cause being dropped when the hint is next reworded.
    """
    text = GROUND_CROSSOVER_HINT.lower()
    assert "ground" in text
    assert "tx" in text and "rx" in text
    assert "rates" in text or "candidate list" in text


def test_the_hint_names_sparse_traffic_as_a_cause():
    """The most likely cause in practice, and the one the hint lacked.

    A later bench session scanned a live Rockchip target and found nothing at
    any rate: wiring correct, 1 500 000 in the ladder, and the board's whole
    boot burst about 55 ms of wire time. The line was simply idle. An
    operator handed only "check ground, check TX/RX, widen the rates" will
    work through three correct-and-useless checks before considering that
    there was nothing to hear.

    Would catch the cause being dropped or softened into a rates suggestion
    when the hint is next reworded.
    """
    text = GROUND_CROSSOVER_HINT.lower()
    assert "idle" in text or "not talking" in text
    assert "boot" in text, "the burst case has to be named, not just idleness"
    assert "power-cycle" in text or "provoke" in text, (
        "naming the cause without naming the action leaves the operator stuck")
