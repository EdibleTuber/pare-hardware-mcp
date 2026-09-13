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
