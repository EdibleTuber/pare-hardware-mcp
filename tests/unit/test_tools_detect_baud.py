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


GAP = {"at_cursor": 42, "duration_s": 12.0, "reason": "baud scan"}


def winner_result(original=9600, final=115200):
    return {
        "samples": {9600: b"\x00\x00garbage\xff", 115200: b"U-Boot\r\nhello\r\n"},
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
    out = json.loads(await tools.console_detect_baud())

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
    out = json.loads(await tools.console_detect_baud())
    winning = next(c for c in out["candidates"] if c["rate"] == 115200)
    assert base64.b64decode(winning["sample_b64"]) == b"U-Boot\r\nhello\r\n"


async def test_no_clear_winner_reports_the_restored_rate(monkeypatch):
    result = {
        "samples": {9600: bytes(range(128, 256)), 115200: bytes(range(128, 256))},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["verdict"] == "no_clear_winner"
    assert "9600" in out["note"]
    assert "hint" not in out


async def test_a_totally_silent_line_names_ground_and_crossover(monkeypatch):
    result = {
        "samples": {9600: b"", 115200: b""},
        "original_baud": 9600, "final_baud": 9600, "restored": True,
        "alive": True, "death_reason": None, "gap": GAP,
    }
    session = FakeSession(scan_result=result)
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["verdict"] == "no_data_at_any_rate"
    assert "ground" in out["hint"].lower()
    assert "tx" in out["hint"].lower() and "rx" in out["hint"].lower()


# --------------------------------------------------------------------------
# Critical 1: a scan's own capture suspension must be visible in its result,
# not only inferable later from console_status/console_read.
# --------------------------------------------------------------------------

async def test_the_scans_own_capture_gap_is_reported(monkeypatch):
    session = FakeSession(scan_result=winner_result())
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["capture_gap"] == GAP


async def test_a_session_error_from_the_scan_is_reported_not_raised(monkeypatch):
    from pare_hardware_mcp.session import SessionError
    session = FakeSession(scan_exc=SessionError("reader thread did not park"))
    install(monkeypatch, session=session)
    out = json.loads(await tools.console_detect_baud())
    assert out["error"]
    assert "did not park" in out["error"]
