# tests/unit/test_baud.py
from __future__ import annotations

import pytest

from pare_hardware_mcp.baud import (BaudScanError, DEFAULT_RATES,
                                    GROUND_CROSSOVER_HINT, MAX_BAUD_RATE,
                                    all_silent, check_budget, pick_winner,
                                    rank_candidates, sanitize_rates,
                                    score_sample)


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
    check_budget(num_rates=4, sample_seconds=1.5, deadline_s=60.0)  # no raise


def test_check_budget_refuses_a_list_that_would_exceed_the_deadline():
    with pytest.raises(BaudScanError) as e:
        check_budget(num_rates=50, sample_seconds=1.5, deadline_s=60.0)
    message = str(e.value)
    assert "75.0" in message and "60.0" in message


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
