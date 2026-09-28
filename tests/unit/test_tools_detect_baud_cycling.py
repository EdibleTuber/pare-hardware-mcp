# tests/unit/test_tools_detect_baud_cycling.py
"""`console_detect_baud_cycling` at the tools.py boundary.

The ACTIVE variant of `console_detect_baud`: for each candidate rate it forces
a fresh boot by power-cycling the target, then captures and scores that boot.
It is the one place both the relay and the console session are used, and it
owns the ordering.

These tests fake BOTH halves the tool coordinates -- the session's
rate-set + windowed capture (via a fake `scan_baud_cycling` that invokes the
tool's `on_rate` callback once per candidate, exactly as the real method does)
and the relay's `power_cycle` (a recorder) -- so no real device is touched.
The reader-thread coordination inside the real method is proven against a pty
in test_session.py, the same way `scan_baud`'s is; this file is the
handler-level contract: preconditions, budget refusal BEFORE any cycle, and
ranked per-rate evidence.

The wrong-rate garbage is DERIVED, not hand-typed: `_uart.misframed` runs a
software UART receiver clocked at the wrong rate over the real Rockchip boot
log, which is what a misconfigured FTDI actually produces.
"""
from __future__ import annotations

import base64
import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.baud import (DEFAULT_CYCLE_CAPTURE_MS,
                                    SAMPLE_PREVIEW_BYTES, looks_like_console,
                                    pick_winner, score_sample)
from pare_hardware_mcp.config import Config
from pare_hardware_mcp.relay import RelayError, RelayNotConfigured

from _uart import BOOT_LOG, ROCKCHIP_CONSOLE_BAUD, misframed

GAP = {"at_cursor": 42, "duration_s": 8.0, "reason": "baud scan (cycling)",
       "in_progress": False}


class FakeSession:
    id = "s-1"
    alive = True
    death_reason = None

    def __init__(self, samples_by_rate=None, cycling_exc=None):
        self.device = type("Dev", (), {
            "by_id": "/dev/serial/by-id/usb-fake-if00-port0"})()
        self._samples_by_rate = samples_by_rate or {}
        self._cycling_exc = cycling_exc
        self.cycling_calls = []

    def scan_baud_cycling(self, rates, capture_seconds, on_rate, decide):
        self.cycling_calls.append((tuple(rates), capture_seconds))
        if self._cycling_exc is not None:
            raise self._cycling_exc
        samples = {}
        for rate in rates:
            # The real method fires the caller's on_rate (a power cycle) once
            # per candidate before sampling; the fake must too, or "one cycle
            # per candidate" is not actually exercised.
            on_rate(rate)
            samples[rate] = self._samples_by_rate.get(rate, b"")
        winner = decide(samples)
        return {
            "samples": samples,
            "rejected": {},
            "listen_seconds": {r: capture_seconds for r in rates},
            "cycles": len(rates),
            "original_baud": 115200,
            "final_baud": winner if winner is not None else 115200,
            "winner": winner,
            "restored": winner is None,
            "alive": True,
            "death_reason": None,
            "gap": GAP,
        }


class FakeManager:
    def __init__(self, session=None):
        self._session = session

    def get(self, session_id):
        if self._session is None or self._session.id != session_id:
            raise KeyError(session_id)
        return self._session


class FakeRelay:
    """Records every power_cycle, so a refused scan can be proven to cycle
    nothing and a run can be proven to cycle once per candidate."""

    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc
        self.power_cycle_calls: list[int] = []

    def power_cycle(self, off_ms):
        self.power_cycle_calls.append(off_ms)
        if self._raise_exc is not None:
            raise self._raise_exc
        return {"status": {"target_power": "on", "channel_state": "idle",
                           "channel": 1, "polarity": "nc"},
                "off_ms_actual": off_ms}


def install(monkeypatch, session=None, relay=None, *, relay_configured=True,
            deadline=60.0, scan_budget=12.0):
    """Wire up a fake manager, config and relay controller.

    A REAL `Config` (not an ad-hoc stand-in) for the same reason the passive
    test uses one: a hand-rolled fake goes on passing when the handler starts
    reading a field it did not have.
    """
    monkeypatch.setattr(tools, "MANAGER", FakeManager(session=session))
    kw = {"request_deadline_s": deadline, "scan_budget_s": scan_budget}
    if relay_configured:
        kw.update(relay_device="/dev/ttyUSB-relay", relay_channel=1,
                  relay_polarity="nc")
    monkeypatch.setattr(tools, "CONFIG", Config(**kw))
    if relay is not None:
        monkeypatch.setattr(
            tools, "RelayController",
            type("_F", (), {"from_config": staticmethod(lambda cfg: relay)}))
    return relay


# --------------------------------------------------------------------------
# Step 1/3: ranked evidence, a power cycle per candidate, and a best rate.
# --------------------------------------------------------------------------

def test_the_fixtures_score_as_the_ranking_test_assumes():
    """Precondition: the derived garbage really does lose to the boot log.

    If `_uart` ever drifts so a misframed vector clears the winner bar, the
    ranking assertions below would pass for the wrong reason. Pin it here.
    """
    real = score_sample(BOOT_LOG)
    assert looks_like_console(real), real
    for rate in (9600, 230400):
        assert not looks_like_console(score_sample(misframed(rate))), rate
    assert pick_winner({9600: misframed(9600), 115200: BOOT_LOG,
                        230400: misframed(230400)}) == 115200


async def test_ranked_evidence_a_cycle_per_candidate_and_a_best_rate(monkeypatch):
    samples = {9600: misframed(9600), ROCKCHIP_CONSOLE_BAUD: BOOT_LOG,
               230400: misframed(230400)}
    relay = FakeRelay()
    session = FakeSession(samples_by_rate=samples)
    install(monkeypatch, session=session, relay=relay)

    rates = [9600, ROCKCHIP_CONSOLE_BAUD, 230400]
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=rates, off_ms=500, capture_ms=500))
    assert "error" not in out, out

    # One power cycle per candidate -- the whole point of the active variant.
    assert relay.power_cycle_calls == [500, 500, 500]

    # Ranked by console_score, descending.
    scores = [c["console_score"] for c in out["candidates"]]
    assert scores == sorted(scores, reverse=True), scores
    assert {c["rate"] for c in out["candidates"]} == set(rates)

    # Every candidate carries its evidence, base64-encoded (untrusted bytes).
    for c in out["candidates"]:
        assert {"rate", "console_score", "printable_ratio", "sample_b64"} <= set(c)
        assert "sample" not in c, "raw bytes must not travel unencoded"
        base64.b64decode(c["sample_b64"])  # does not raise

    # The real boot log rate wins and leads the ranking.
    assert out["best"] == {"rate": ROCKCHIP_CONSOLE_BAUD}
    assert out["candidates"][0]["rate"] == ROCKCHIP_CONSOLE_BAUD


async def test_the_winning_sample_round_trips_through_base64(monkeypatch):
    samples = {9600: misframed(9600), ROCKCHIP_CONSOLE_BAUD: BOOT_LOG}
    session = FakeSession(samples_by_rate=samples)
    install(monkeypatch, session=session, relay=FakeRelay())
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, ROCKCHIP_CONSOLE_BAUD],
        off_ms=500, capture_ms=500))
    winning = next(c for c in out["candidates"]
                   if c["rate"] == ROCKCHIP_CONSOLE_BAUD)
    # The sample travels as a bounded preview (SAMPLE_PREVIEW_BYTES), the same
    # way the passive tool truncates it -- so it is the prefix of the boot log,
    # flagged truncated because the log is longer.
    assert base64.b64decode(winning["sample_b64"]) == BOOT_LOG[:SAMPLE_PREVIEW_BYTES]
    assert winning["sample_truncated"] is True


async def test_the_default_capture_window_is_used_when_none_is_given(monkeypatch):
    samples = {9600: misframed(9600), ROCKCHIP_CONSOLE_BAUD: BOOT_LOG}
    session = FakeSession(samples_by_rate=samples)
    # Budget must fit the default window: two rates * (0/off + 2s + margin).
    install(monkeypatch, session=session, relay=FakeRelay(),
            deadline=60.0, scan_budget=60.0)
    await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, ROCKCHIP_CONSOLE_BAUD], off_ms=500)
    (_rates, capture_seconds), = session.cycling_calls
    assert capture_seconds == DEFAULT_CYCLE_CAPTURE_MS / 1000.0


# --------------------------------------------------------------------------
# Step 4 (Review Focus): budget refused BEFORE the first power cycle.
# --------------------------------------------------------------------------

async def test_a_budget_exceeding_scan_is_refused_and_cycles_nothing(monkeypatch):
    relay = FakeRelay()
    session = FakeSession(samples_by_rate={9600: BOOT_LOG})
    # 3 rates * (3s off + 2s capture + margin) far exceeds a 1s budget.
    install(monkeypatch, session=session, relay=relay,
            deadline=1.0, scan_budget=1.0)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200, 230400],
        off_ms=3000, capture_ms=2000))
    assert out["error"]
    assert "budget" in out["error"].lower()
    assert relay.power_cycle_calls == [], "a refused scan must cycle nothing"
    assert session.cycling_calls == [], "the session must not be touched either"


async def test_the_budget_uses_the_tighter_of_scan_budget_and_deadline(monkeypatch):
    """The refusal is against min(scan_budget_s, request_deadline_s): a
    generous scan budget must not let a scan run past a tight deadline."""
    relay = FakeRelay()
    session = FakeSession(samples_by_rate={9600: BOOT_LOG})
    install(monkeypatch, session=session, relay=relay,
            deadline=1.0, scan_budget=600.0)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200], off_ms=3000, capture_ms=2000))
    assert out["error"]
    assert relay.power_cycle_calls == []


async def test_a_negative_capture_ms_cannot_defeat_the_budget_refusal(monkeypatch):
    """An adversarial capture_ms must not shrink the worst-case estimate and
    slip past the refusal into cycling the target N times."""
    relay = FakeRelay()
    session = FakeSession(samples_by_rate={9600: BOOT_LOG})
    install(monkeypatch, session=session, relay=relay,
            deadline=1.0, scan_budget=1.0)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200, 230400],
        off_ms=3000, capture_ms=-100000))
    assert out["error"]
    assert relay.power_cycle_calls == []


async def test_a_scan_that_fits_the_budget_runs(monkeypatch):
    relay = FakeRelay()
    session = FakeSession(samples_by_rate={9600: misframed(9600),
                                           ROCKCHIP_CONSOLE_BAUD: BOOT_LOG})
    install(monkeypatch, session=session, relay=relay,
            deadline=60.0, scan_budget=12.0)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, ROCKCHIP_CONSOLE_BAUD],
        off_ms=1000, capture_ms=1000))
    assert "error" not in out, out
    assert relay.power_cycle_calls == [1000, 1000]


# --------------------------------------------------------------------------
# Step 5: evidence is surfaced even when nothing wins.
# --------------------------------------------------------------------------

async def test_evidence_is_surfaced_even_when_nothing_wins(monkeypatch):
    samples = {9600: misframed(9600), 115200: misframed(115200)}
    # Precondition: neither derived vector clears the winner bar.
    assert pick_winner(samples) is None
    relay = FakeRelay()
    session = FakeSession(samples_by_rate=samples)
    install(monkeypatch, session=session, relay=relay)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200], off_ms=500, capture_ms=500))
    assert "error" not in out, out
    assert out["best"] is None, "nothing cleared the bar, so best must be null"
    # Every candidate still appears, with its (low) score and sample, so the
    # operator can eyeball them during enumeration.
    assert {c["rate"] for c in out["candidates"]} == {9600, 115200}
    for c in out["candidates"]:
        assert "sample_b64" in c
        assert "console_score" in c
    # The target was still cycled once per candidate to get the evidence.
    assert relay.power_cycle_calls == [500, 500]


# --------------------------------------------------------------------------
# Step 6: requires a relay AND an open, alive session -- else refuse, no cycle.
# --------------------------------------------------------------------------

async def test_no_relay_configured_is_refused_and_cycles_nothing(monkeypatch):
    session = FakeSession(samples_by_rate={9600: BOOT_LOG})
    # No relay fields on the Config, and the REAL RelayController.from_config,
    # so this exercises the actual RelayNotConfigured path.
    install(monkeypatch, session=session, relay=None, relay_configured=False)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600], off_ms=500, capture_ms=500))
    assert out["error"]
    assert "relay" in out["error"].lower()
    assert session.cycling_calls == []


async def test_no_open_session_is_refused_and_cycles_nothing(monkeypatch):
    relay = FakeRelay()
    install(monkeypatch, session=None, relay=relay)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600], off_ms=500, capture_ms=500))
    assert out["error"]
    assert relay.power_cycle_calls == []


async def test_a_dead_session_is_refused_and_cycles_nothing(monkeypatch):
    relay = FakeRelay()
    session = FakeSession(samples_by_rate={9600: BOOT_LOG})
    session.alive = False
    session.death_reason = "device vanished"
    install(monkeypatch, session=session, relay=relay)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600], off_ms=500, capture_ms=500))
    assert out["error"]
    assert "device vanished" in out["error"]
    assert relay.power_cycle_calls == []
    assert session.cycling_calls == []


# --------------------------------------------------------------------------
# Errors from the two coordinated devices are reported, never raised.
# --------------------------------------------------------------------------

async def test_a_relay_error_mid_scan_is_reported_without_the_at_protocol(monkeypatch):
    """A relay failure must surface as `safe_message`, never a raw AT string."""
    relay = FakeRelay(raise_exc=RelayError(
        "relay command 'AT+CH1=0' failed: timeout",
        safe_message="relay at /dev/ttyUSB-relay did not confirm the command"))
    session = FakeSession(samples_by_rate={9600: BOOT_LOG}, cycling_exc=None)

    # Make the session propagate the relay error out of on_rate, as the real
    # method does (the callback is invoked inside it and its exception escapes).
    def scan_baud_cycling(rates, capture_seconds, on_rate, decide):
        session.cycling_calls.append((tuple(rates), capture_seconds))
        on_rate(rates[0])  # fires the failing power cycle
        raise AssertionError("unreachable: on_rate should have raised")

    session.scan_baud_cycling = scan_baud_cycling
    install(monkeypatch, session=session, relay=relay)
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200], off_ms=500, capture_ms=500))
    assert out["error"]
    assert "AT+CH" not in out["error"], "the wire protocol must never leak"
    assert "did not confirm" in out["error"]


async def test_a_session_error_from_the_scan_is_reported_not_raised(monkeypatch):
    from pare_hardware_mcp.session import SessionError
    session = FakeSession(cycling_exc=SessionError("reader thread did not park"))
    install(monkeypatch, session=session, relay=FakeRelay())
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, 115200], off_ms=500, capture_ms=500))
    assert out["error"]
    assert "did not park" in out["error"]


# --------------------------------------------------------------------------
# The scan's own capture gap and how the budget was spent reach the wire.
# --------------------------------------------------------------------------

async def test_the_capture_gap_is_reported(monkeypatch):
    session = FakeSession(samples_by_rate={9600: misframed(9600),
                                           ROCKCHIP_CONSOLE_BAUD: BOOT_LOG})
    install(monkeypatch, session=session, relay=FakeRelay())
    out = json.loads(await tools.console_detect_baud_cycling(
        session="s-1", rates=[9600, ROCKCHIP_CONSOLE_BAUD],
        off_ms=500, capture_ms=500))
    assert out["capture_gap"] == GAP
