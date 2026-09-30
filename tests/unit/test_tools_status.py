# tests/unit/test_tools_status.py
from __future__ import annotations

import json

import pytest

from pare_hardware_mcp import tools


async def test_list_devices_reports_by_id_and_serial_but_never_voltage(monkeypatch):
    from pare_hardware_mcp.devices import ResolvedDevice
    monkeypatch.setattr(tools, "list_serial_devices", lambda *a, **k: [
        ResolvedDevice(by_id="/dev/serial/by-id/usb-...-if01-port0",
                       tty="/dev/ttyUSB1", serial="TG1119e7", interface="if01"),
    ])
    out = json.loads(await tools.list_devices())
    assert out["devices"][0]["serial"] == "TG1119e7"
    # The Tigard's level selector is a physical switch with no software
    # read-back. Reporting a value an operator might trust would be a guess.
    assert "voltage" not in json.dumps(out).lower()


async def test_bench_status_reports_a_missing_artifact_root_as_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", str(tmp_path / "nope"))
    out = json.loads(await tools.bench_status())
    assert out["artifact_root"]["present"] is False


async def test_bench_status_reads_the_drive_id_when_present(monkeypatch, tmp_path):
    (tmp_path / ".bench-store-id").write_text("0361c41f-e680-4d4e-b9c3-39af8a33d067\n")
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", str(tmp_path))
    out = json.loads(await tools.bench_status())
    assert out["artifact_root"]["present"] is True
    assert out["artifact_root"]["drive_id"] == "0361c41f-e680-4d4e-b9c3-39af8a33d067"


async def test_bench_status_works_with_no_session_open(monkeypatch):
    out = json.loads(await tools.bench_status())
    assert "session" in out


async def test_bench_status_reports_the_configured_relay(monkeypatch):
    # Spec §5: when the relay is configured, the operator must be able to see
    # the relay adapter the same way they see the Tigard. The relay is
    # by-path (a generic CP2102 serial), so it does not self-label in
    # list_devices' by-id entries -- this field is what tells the operator
    # which enumerated adapter is the relay, plus its channel and polarity.
    monkeypatch.setenv(
        "PARE_HW_RELAY_DEVICE",
        "/dev/serial/by-path/platform-xhci-hcd.0-usb-0:2:1.0-port0",
    )
    monkeypatch.setenv("PARE_HW_RELAY_CHANNEL", "1")
    monkeypatch.setenv("PARE_HW_RELAY_POLARITY", "nc")
    out = json.loads(await tools.bench_status())
    assert out["relay"] == {
        "device": "/dev/serial/by-path/platform-xhci-hcd.0-usb-0:2:1.0-port0",
        "channel": 1,
        "polarity": "nc",
    }


@pytest.mark.parametrize(
    "env",
    [
        {},  # nothing set
        {"PARE_HW_RELAY_DEVICE": "/dev/serial/by-path/x-port0"},  # partial
        {"PARE_HW_RELAY_DEVICE": "/dev/serial/by-path/x-port0",
         "PARE_HW_RELAY_CHANNEL": "1"},  # partial
    ],
)
async def test_bench_status_relay_is_null_when_not_fully_configured(monkeypatch, env):
    # "Configured" means the power tools can actually use it -- all three
    # fields, matching RelayNotConfigured's semantics. A partial declaration
    # is a misconfiguration the power tools already name explicitly;
    # bench_status must not present a half-set relay as a working one.
    for var in ("PARE_HW_RELAY_DEVICE", "PARE_HW_RELAY_CHANNEL",
                "PARE_HW_RELAY_POLARITY"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    out = json.loads(await tools.bench_status())
    assert out["relay"] is None
