# tests/unit/test_tools_power.py
"""power_status/power_set/power_cycle -- thin async wrappers over RelayController.

Every handler must resolve `RelayController.from_config(CONFIG)`, run the
blocking relay call via `asyncio.to_thread`, and NEVER raise out of the
handler: `RelayNotConfigured` becomes a clear "no relay configured" `_err`,
`RelayError` becomes an `_err` naming the failure, success becomes `_ok`
carrying the status. Tests inject a fake controller by monkeypatching
`tools.CONFIG` and `tools.RelayController` (the symbol imported into
`tools.py`), so no real device is ever touched.
"""
from __future__ import annotations

import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.config import Config
from pare_hardware_mcp.relay import RelayError, RelayNotConfigured

RELAY_CONFIGURED = Config(relay_device="/dev/ttyUSB-relay", relay_channel=1,
                          relay_polarity="nc")


class FakeRelayController:
    """Records calls, returns scripted results, optionally raises."""

    def __init__(self, status=None, set_power_result=None,
                 power_cycle_result=None, raise_exc=None):
        self._status = status or {
            "target_power": "on", "channel_state": "idle",
            "channel": 1, "polarity": "nc",
        }
        self._set_power_result = set_power_result
        self._power_cycle_result = power_cycle_result
        self._raise_exc = raise_exc
        self.set_power_calls: list[str] = []
        self.power_cycle_calls: list[int] = []
        self.status_calls = 0

    def status(self) -> dict:
        self.status_calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._status

    def set_power(self, state: str) -> dict:
        self.set_power_calls.append(state)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._set_power_result is not None:
            return self._set_power_result
        on = state == "on"
        return {"target_power": "on" if on else "off",
                "channel_state": "idle" if on else "energised",
                "channel": 1, "polarity": "nc"}

    def power_cycle(self, off_ms: int) -> dict:
        self.power_cycle_calls.append(off_ms)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._power_cycle_result is not None:
            return self._power_cycle_result
        return {"status": self._status, "off_ms_actual": off_ms}


def _patch_controller(monkeypatch, fake: FakeRelayController):
    monkeypatch.setattr(tools, "CONFIG", RELAY_CONFIGURED)
    monkeypatch.setattr(tools, "RelayController",
                        type("_F", (), {"from_config": staticmethod(lambda cfg: fake)}))


async def test_power_set_success_reports_the_new_state(monkeypatch):
    fake = FakeRelayController()
    _patch_controller(monkeypatch, fake)

    out = json.loads(await tools.power_set("off"))

    assert out["target_power"] == "off"
    assert fake.set_power_calls == ["off"]


async def test_power_status_success_reports_the_status(monkeypatch):
    fake = FakeRelayController(status={
        "target_power": "on", "channel_state": "energised",
        "channel": 2, "polarity": "no",
    })
    _patch_controller(monkeypatch, fake)

    out = json.loads(await tools.power_status())

    assert out["target_power"] == "on"
    assert out["channel"] == 2
    assert fake.status_calls == 1


async def test_power_status_with_no_relay_configured_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(tools, "CONFIG", Config())  # relay fields all None

    out = json.loads(await tools.power_status())

    assert "error" in out
    assert "relay" in out["error"].lower()
    assert "configur" in out["error"].lower()


async def test_power_set_with_no_relay_configured_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(tools, "CONFIG", Config())

    out = json.loads(await tools.power_set("on"))

    assert "error" in out
    assert "configur" in out["error"].lower()


async def test_power_cycle_with_no_relay_configured_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(tools, "CONFIG", Config())

    out = json.loads(await tools.power_cycle())

    assert "error" in out
    assert "configur" in out["error"].lower()


async def test_power_cycle_relay_error_becomes_an_err_not_a_crash(monkeypatch):
    fake = FakeRelayController(raise_exc=RelayError("relay open failed: /dev/relay"))
    _patch_controller(monkeypatch, fake)

    out = json.loads(await tools.power_cycle())

    assert "error" in out
    assert "relay open failed: /dev/relay" in out["error"]


async def test_power_set_relay_error_becomes_an_err_not_a_crash(monkeypatch):
    fake = FakeRelayController(raise_exc=RelayError("relay command failed"))
    _patch_controller(monkeypatch, fake)

    out = json.loads(await tools.power_set("on"))

    assert "error" in out
    assert "relay command failed" in out["error"]


async def test_power_cycle_reports_the_restored_on_resting_state(monkeypatch):
    fake = FakeRelayController(power_cycle_result={
        "status": {"target_power": "on", "channel_state": "idle",
                   "channel": 1, "polarity": "nc"},
        "off_ms_actual": 3000,
    })
    _patch_controller(monkeypatch, fake)

    out = json.loads(await tools.power_cycle())

    assert out["target_power"] == "on"
    assert out["off_ms_actual"] == 3000


async def test_power_cycle_passes_off_ms_through_to_the_controller(monkeypatch):
    fake = FakeRelayController()
    _patch_controller(monkeypatch, fake)

    await tools.power_cycle(off_ms=1500)

    assert fake.power_cycle_calls == [1500]


def test_power_tools_are_registered_at_risk_tier_high():
    from pare_hardware_mcp.contract import TOOL_SPECS
    tiers = {s.name: s.risk_tier for s in TOOL_SPECS}
    for name in ("power_status", "power_set", "power_cycle"):
        assert name in tiers, name
        assert tiers[name] == "high", name
