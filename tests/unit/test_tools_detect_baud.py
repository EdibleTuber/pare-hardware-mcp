# tests/unit/test_tools_detect_baud.py
"""`console_detect_baud` at the tools.py boundary, against a fake session.

The mechanics of the scan itself (pause the reader, never reopen, never
transmit) are `session.py`'s job and are tested against a real pty in
test_session.py. This file is about the handler-level contract: it requires
an already-open session (the ruling that resolves the brief's `device` gap),
it never opens one of its own, it checks the scan budget against the
configured deadline before calling into the session, and it shapes the
result -- ranked candidates, base64 samples, and the three verdicts.
"""
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.baud import (WINNER_PRINTABLE_THRESHOLD,
                                    WIRING_SUSPECT_PRINTABLE_MAX,
                                    score_sample)


class FakeSession:
    id = "s-1"
    alive = True

    def __init__(self, device_by_id="/dev/serial/by-id/usb-fake-if00-port0",
                 scan_result=None, scan_exc=None):
        self.device = type("Dev", (), {"by_id": device_by_id})()
        self._scan_result = scan_result
        self._scan_exc = scan_exc
        self.scan_calls = []

    def scan_baud(self, rates, sample_seconds, decide):
        self.scan_calls.append((rates, sample_seconds))
        if self._scan_exc is not None:
            raise self._scan_exc
        return self._scan_result


class FakeManager:
    def __init__(self, current=None):
        self.current = current


def install(monkeypatch, session=None, deadline=60.0):
    manager = FakeManager(current=session)
    monkeypatch.setattr(tools, "MANAGER", manager)
    monkeypatch.setattr(tools, "CONFIG",
                        type("Cfg", (), {"request_deadline_s": deadline})())
    return manager


GAP = {"at_cursor": 42, "duration_s": 12.0, "reason": "baud scan", "in_progress": False}


def winner_result(original=9600, final=115200):
    return {
        "samples": {9600: b"\x00\x00garbage\xff", 115200: b"U-Boot\r\nhello\r\n"},
        "rejected": {},
        "original_baud": original,
        "final_baud": final,
        "restored": final == original,
        "alive": True,
        "death_reason": None,
        "gap": GAP,
    }


# --------------------------------------------------------------------------
# The ruling: requires an open session, never opens one of its own.
# --------------------------------------------------------------------------

async def test_no_session_open_is_a_named_refusal_not_an_attempted_open(monkeypatch):
    install(monkeypatch, session=None)
    out = json.loads(await tools.console_detect_baud(device="/dev/whatever"))
    assert out["error"]
    assert "console_open" in out["error"]


async def test_a_device_argument_that_does_not_match_the_open_session_is_refused(monkeypatch):
    session = FakeSession(device_by_id="/dev/serial/by-id/the-real-one")
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(device="/dev/serial/by-id/a-different-one"))
    assert out["error"]
    assert "the-real-one" in out["error"] and "a-different-one" in out["error"]
    assert session.scan_calls == [], "must not scan when the device argument disagrees"


async def test_a_matching_device_argument_is_accepted(monkeypatch):
    session = FakeSession(device_by_id="/dev/serial/by-id/x", scan_result=winner_result())
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(device="/dev/serial/by-id/x"))
    assert "error" not in out


async def test_a_dead_session_is_refused_before_scanning(monkeypatch):
    session = FakeSession()
    session.alive = False
    session.death_reason = "device vanished"
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["error"]
    assert "device vanished" in out["error"]
    assert session.scan_calls == []


# --------------------------------------------------------------------------
# Invariant 4: the budget is checked before the session is ever touched.
# --------------------------------------------------------------------------

async def test_a_rate_list_exceeding_the_deadline_is_refused_before_scanning(monkeypatch):
    session = FakeSession()
    install(monkeypatch, session=session, deadline=1.0)
    out = json.loads(await tools.console_detect_baud(
        rates=[9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]))
    assert out["error"]
    assert "deadline" in out["error"]
    assert session.scan_calls == [], "the port must not be touched by a refused scan"


async def test_zero_is_filtered_rather_than_reaching_the_session(monkeypatch):
    session = FakeSession(scan_result=winner_result())
    install(monkeypatch, session=session)
    await tools.console_detect_baud(rates=[0, 9600, 115200])
    (rates, _sample_seconds), = session.scan_calls
    assert 0 not in rates


async def test_a_rate_list_of_only_invalid_values_is_refused(monkeypatch):
    session = FakeSession()
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[0, -5]))
    assert out["error"]
    assert session.scan_calls == []


# --------------------------------------------------------------------------
# Result shape: ranked evidence, base64 samples, and the three verdicts.
# --------------------------------------------------------------------------

async def test_a_winner_is_reported_and_the_session_is_left_there(monkeypatch):
    session = FakeSession(scan_result=winner_result(original=9600, final=115200))
    install(monkeypatch, session=session)
    # Matches winner_result()'s two sampled rates exactly: the verdict logic
    # gates on every REQUESTED candidate having actually been attempted
    # (samples + rejected), so a mismatched rates= here would misreport this
    # as an aborted scan even though the fake never aborted anything.
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))

    assert out["verdict"] == "winner"
    assert out["final_baud"] == 115200
    assert out["restored"] is False
    assert "115200" in out["note"]

    rates_seen = {c["rate"] for c in out["candidates"]}
    assert rates_seen == {9600, 115200}
    for candidate in out["candidates"]:
        assert "sample" not in candidate, "raw bytes must not travel unencoded"
        base64.b64decode(candidate["sample_b64"])  # does not raise
    # Best candidate first.
    assert out["candidates"][0]["rate"] == 115200


async def test_a_winner_sample_round_trips_through_base64(monkeypatch):
    session = FakeSession(scan_result=winner_result())
    install(monkeypatch, session=session)
    # Matches winner_result()'s two sampled rates -- see the comment on
    # test_a_winner_is_reported_and_the_session_is_left_there for why a
    # mismatch here would silently exercise the scan_aborted branch instead.
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    winning = next(c for c in out["candidates"] if c["rate"] == 115200)
    assert base64.b64decode(winning["sample_b64"]) == b"U-Boot\r\nhello\r\n"


async def test_every_candidate_scoring_poorly_names_ground_and_crossover(monkeypatch):
    """The blocker this file previously pinned the wrong way round.

    `bytes(range(128, 256))` is a floating ground's signature -- high-bit
    noise, `printable_ratio` 0.0 at every candidate. The hint used to be
    gated on `all_silent(samples)` alone, so this case (bytes DID arrive,
    none of them text-like) got "no candidate rate looked like clean console
    text" and nothing else: the operator is sent off to try more rates while
    the fault is a wire. Framing errors from a floating ground and a
    mis-framed rate produce the same evidence, and `score_sample` cannot
    separate them, which is exactly why the hint belongs here.

    Would catch: the hint gated on silence, on `all_silent`, or on `not
    samples`. The verdict and note are unchanged -- the hint is additive.
    """
    result = {
        "samples": {9600: bytes(range(128, 256)), 115200: bytes(range(128, 256))},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "no_clear_winner"
    assert "9600" in out["note"]
    assert "ground" in out["hint"].lower()
    assert "tx" in out["hint"].lower() and "rx" in out["hint"].lower()
    # Ranked evidence still travels: the hint adds to the response, it does
    # not stand in for the scores.
    assert {c["rate"] for c in out["candidates"]} == {9600, 115200}
    assert all(c["printable_ratio"] == 0.0 for c in out["candidates"])


async def test_a_marginal_no_clear_winner_does_not_accuse_the_wiring(monkeypatch):
    """The other side of the threshold, and the reason it is not 0.85.

    These samples are mostly-printable text that simply did not clear
    `WINNER_PRINTABLE_THRESHOLD` -- a rate near the right one, worth
    retrying. Would catch: the hint attached to every `restored` verdict, or
    a `WIRING_SUSPECT_PRINTABLE_MAX` raised to the winner threshold, which
    would fire on every ordinary missed-rate scan and train a caller to
    ignore it.
    """
    marginal = b"U-Boot 2021.01 \r\n" * 6 + bytes(range(128, 160))
    ratio = score_sample(marginal)["printable_ratio"]
    assert WIRING_SUSPECT_PRINTABLE_MAX < ratio < WINNER_PRINTABLE_THRESHOLD, ratio

    result = {
        "samples": {9600: marginal, 115200: marginal},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "no_clear_winner"
    assert "hint" not in out


async def test_one_readable_candidate_among_noise_does_not_accuse_the_wiring(monkeypatch):
    """A mixed scan: one candidate is high-bit noise, the other is readable
    enough to rule wiring out but not enough to win.

    The pairing matters. A scan where SOME rate produced text-like bytes is
    evidence the link works and the rate list is what needs widening -- the
    opposite of the wiring case. Would catch `any(...)` written where
    `all(...)` belongs: the noise candidate alone would then be enough to
    accuse the wire. (A mixed scan with an outright WINNER would not catch
    that mutation, because the hint branch is never reached on the winner
    path -- which is why this fixture is deliberately `restored: True`.)
    """
    marginal = b"U-Boot 2021.01 \r\n" * 6 + bytes(range(128, 160))
    assert (WIRING_SUSPECT_PRINTABLE_MAX
            < score_sample(marginal)["printable_ratio"]
            < WINNER_PRINTABLE_THRESHOLD)
    result = {
        "samples": {9600: bytes(range(128, 256)), 115200: marginal},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "no_clear_winner"
    assert "hint" not in out


async def test_a_device_that_died_on_the_last_candidate_is_not_a_clean_verdict(monkeypatch):
    """Every candidate WAS attempted, so the count-based `scan_aborted` check
    does not fire -- but the link failed during the scan, and the samples the
    dying candidates produced are evidence about a dead device.

    Before this, the response carried `alive: False` and a `death_reason`
    next to a completed verdict and the wiring hint: an operator told to
    inspect the ground on an adapter that is simply gone. Would catch:
    falling through to `no_data_at_any_rate`/`no_clear_winner`. The
    companion test below is what catches the branch being gated on the racy
    `alive` flag instead -- this one alone would not, since `alive` is False
    here too.
    """
    gone = "device /dev/serial/by-id/usb-fake-if00-port0 disappeared during a baud scan at 115200"
    result = {
        "samples": {9600: b"boot\r\n", 115200: b""},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": False, "death_reason": gone, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "scan_device_died"
    assert gone in out["note"]
    assert "hint" not in out, "a device that is gone must not draw a wiring hint"
    assert out["death_reason"] == gone


async def test_a_close_racing_the_response_does_not_read_as_a_device_death(monkeypatch):
    """`alive` can read False for a session that never failed.

    `scan_baud` reads `self.alive` after releasing `_write_lock` -- which is
    exactly what a racing `_shutdown` was waiting on -- and `_shutdown` sets
    `_alive = False` without ever touching `_death_reason` (session.py's
    `_die` is the only writer of that). So a completed scan can hand back
    `alive: False, death_reason: None`, and calling that a device death
    would tell an operator their adapter failed when the daemon merely
    closed the session.

    Would catch: the new branch gated on `not result["alive"]` rather than
    on `death_reason is not None`.
    """
    result = {
        "samples": {9600: bytes(range(128, 256)),
                    115200: b"U-Boot 2021.01\r\nHit any key\r\n"},
        "rejected": {},
        "original_baud": 9600, "final_baud": 115200, "restored": False,
        "alive": False, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "winner"
    assert out["alive"] is False


async def test_a_device_that_died_after_silent_candidates_is_not_a_wiring_verdict(monkeypatch):
    """The specific collision: every sample empty AND the device gone.

    Silence alone is `no_data_at_any_rate` + the hint. Silence caused by an
    adapter that left the bus is not a wiring question at all, and
    `death_reason` is what tells them apart. Would catch: the death check
    placed AFTER `all_silent` in the chain.
    """
    gone = "device /dev/serial/by-id/usb-fake-if00-port0 disappeared during a baud scan at 9600"
    result = {
        "samples": {9600: b"", 115200: b""},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": False, "death_reason": gone, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "scan_device_died"
    assert "hint" not in out


async def test_an_aborted_scan_names_the_death_when_there_was_one(monkeypatch):
    """The partial case keeps its count-based verdict but stops saying
    "closing or disappeared" when it knows which. Would catch a death branch
    placed BEFORE the attempted-vs-requested check, which would lose the
    count."""
    gone = "device /dev/x disappeared during a baud scan at 9600"
    result = {
        "samples": {9600: b""},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": False, "death_reason": gone, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200, 230400]))
    assert out["verdict"] == "scan_aborted"
    assert "1 of 3" in out["note"]
    assert gone in out["note"]
    assert "hint" not in out


async def test_a_totally_silent_line_names_ground_and_crossover(monkeypatch):
    result = {
        "samples": {9600: b"", 115200: b""},
        "rejected": {},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "no_data_at_any_rate"
    assert "ground" in out["hint"].lower()
    assert "tx" in out["hint"].lower() and "rx" in out["hint"].lower()


async def test_all_candidates_rejected_is_distinguished_from_a_silent_line(monkeypatch):
    """The reviewer's N4 fix: rejection is per-candidate, not scan-aborting.

    When EVERY candidate was rejected before any byte could be sampled, the
    ground/TX-RX-crossover hint would be actively misleading -- the problem
    is the rate list, not the wiring -- so this must be a distinct verdict.
    """
    result = {
        "samples": {},
        "rejected": {250000: "kernel rejected it", 999999: "kernel rejected it"},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    # Matches the two rejected rates exactly -- see the comment on the
    # winner test above for why a mismatch here would misreport this.
    out = json.loads(await tools.console_detect_baud(rates=[250000, 999999]))
    assert out["verdict"] == "all_candidate_rates_rejected"
    assert "hint" not in out
    # A list of {rate, reason}, matching `candidates`' shape -- not
    # session.py's {rate: reason} dict, whose int keys would otherwise
    # travel over JSON coerced to strings while `candidates[]["rate"]`
    # stays an int for the same kind of value in the same response.
    assert out["rejected"] == [
        {"rate": 250000, "reason": "kernel rejected it"},
        {"rate": 999999, "reason": "kernel rejected it"},
    ]


async def test_a_partial_rejection_still_reports_ranked_evidence(monkeypatch):
    """One bad candidate must not throw away another candidate's good sample."""
    result = {
        "samples": {9600: b"U-Boot\r\nhello\r\n"},
        "rejected": {250000: "kernel rejected it"},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 250000]))
    assert out["verdict"] != "all_candidate_rates_rejected"
    assert out["verdict"] != "scan_aborted"
    assert {c["rate"] for c in out["candidates"]} == {9600}
    assert out["rejected"] == [{"rate": 250000, "reason": "kernel rejected it"}]


# --------------------------------------------------------------------------
# New: a scan that stopped early (a concurrent close, or the device dying
# mid-scan) must not be misreported as one of the completed verdicts above.
# --------------------------------------------------------------------------

async def test_a_scan_aborted_partway_gets_its_own_verdict(monkeypatch):
    """Reproduces the reviewer's finding: samples={}, one rejection recorded,
    then the loop broke on _stop/_closing before trying the rest. Before this
    fix, `not samples and rejected` alone would have called this
    "all_candidate_rates_rejected" -- false, because the untried rates were
    never even attempted, let alone rejected.
    """
    result = {
        "samples": {},
        "rejected": {333333: "kernel rejected it"},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True,  # racy and still True at this instant -- must not be relied on
        "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    # Three requested, only one ever attempted (and rejected).
    out = json.loads(await tools.console_detect_baud(rates=[9600, 333333, 115200]))
    assert out["verdict"] == "scan_aborted"
    assert "1 of 3" in out["note"]
    assert "hint" not in out


async def test_a_fully_completed_all_rejected_scan_is_not_misreported_as_aborted(monkeypatch):
    """The companion case: every candidate WAS attempted, so this is the
    real "all_candidate_rates_rejected" verdict, not "scan_aborted"."""
    result = {
        "samples": {},
        "rejected": {9600: "x", 115200: "x"},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["verdict"] == "all_candidate_rates_rejected"


# --------------------------------------------------------------------------
# Critical 1: a scan's own capture suspension must be visible in its result,
# not only inferable later from console_status/console_read.
# --------------------------------------------------------------------------

async def test_the_scans_own_capture_gap_is_reported(monkeypatch):
    session = FakeSession(scan_result=winner_result())
    install(monkeypatch, session=session)
    # Matches winner_result()'s two sampled rates -- see the comment on
    # test_a_winner_is_reported_and_the_session_is_left_there for why a
    # mismatch here would silently exercise the scan_aborted branch instead.
    out = json.loads(await tools.console_detect_baud(rates=[9600, 115200]))
    assert out["capture_gap"] == GAP


async def test_a_session_error_from_the_scan_is_reported_not_raised(monkeypatch):
    from pare_hardware_mcp.session import SessionError
    session = FakeSession(scan_exc=SessionError("reader thread did not park"))
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["error"]
    assert "did not park" in out["error"]
