# tests/unit/test_baud.py
from __future__ import annotations

import random

import pytest

import _uart
from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_DWELL_SECONDS,
                                    DEFAULT_RATES,
                                    DEFAULT_SCAN_BUDGET_SECONDS,
                                    GROUND_CROSSOVER_HINT, MAX_BAUD_RATE,
                                    WINNER_CONSOLE_THRESHOLD,
                                    WINNER_PRINTABLE_THRESHOLD,
                                    WIRING_SUSPECT_CONSOLE_MAX,
                                    all_scored_poorly, all_silent,
                                    check_budget, looks_like_console,
                                    pick_winner, rank_candidates,
                                    sanitize_rates, score_sample)
from pare_hardware_mcp.config import Config

# Every wrong rate in the default ladder, across a spread of transmitter
# inter-byte timings. Built once: it is ~0.5s of simulation.
MISFRAMED = [
    (gap, rate, _uart.misframed(rate, gap))
    for gap in (0, 1, 2, 3, 5, 8)
    for rate in DEFAULT_RATES
    if rate != _uart.ROCKCHIP_CONSOLE_BAUD
]
MISFRAMED = [(gap, rate, data) for gap, rate, data in MISFRAMED if data]


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
    own request deadline. Uses `Config()`'s real default deadline rather than
    a number typed here, so it tracks the worker rather than a copy of it.
    """
    check_budget(len(DEFAULT_RATES), DEFAULT_DWELL_SECONDS,
                 DEFAULT_SCAN_BUDGET_SECONDS, Config().request_deadline_s)


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
    ratios = [score_sample(d)["printable_ratio"] for _, _, d in MISFRAMED]
    assert min(ratios) < 0.75 < max(ratios), (min(ratios), max(ratios))


def test_printable_ratio_alone_cannot_separate_garbage_from_console_text():
    """The defect, stated as a test: the OLD discriminator fails here.

    Some misframed samples score above `WIRING_SUSPECT_PRINTABLE_MAX` (0.5,
    the bar the old code used) and would therefore have been read as "a
    worse rate, not a wiring problem", and some come within a few points of
    `WINNER_PRINTABLE_THRESHOLD`. This test does not assert the fix -- it
    pins the reason for it, so a future change that quietly reverts to
    printable-only scoring has something to fail against.
    """
    ratios = [score_sample(d)["printable_ratio"] for _, _, d in MISFRAMED]
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
    worst = max(score_sample(d)["console_score"] for _, _, d in MISFRAMED)
    assert genuine > 0.74, genuine
    assert worst < WIRING_SUSPECT_CONSOLE_MAX, worst
    assert genuine > 5 * worst, (genuine, worst)


def test_no_misframed_sample_is_ever_crowned_a_winner():
    """The failure that would leave the port applied at a wrong rate.

    Every misframed sample, presented alone so there is nothing better to
    lose to. Would catch a winner rule that ranks by comparison instead of
    against an absolute bar, and any threshold set below the top of the
    garbage population.
    """
    for gap, rate, data in MISFRAMED:
        assert pick_winner({rate: data}) is None, (gap, rate,
                                                   score_sample(data))


def test_the_genuine_rate_wins_against_the_whole_misframed_field():
    """The bench's actual scan, end to end through the scorer: the real rate
    against every wrong rate in the ladder, all sampled at once."""
    samples = {rate: data for _, rate, data in MISFRAMED if rate != 1500000}
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
    Would catch the third cause being dropped when the hint is next reworded.
    """
    text = GROUND_CROSSOVER_HINT.lower()
    assert "ground" in text
    assert "tx" in text and "rx" in text
    assert "rates" in text or "candidate list" in text
