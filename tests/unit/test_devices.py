# tests/unit/test_devices.py
from __future__ import annotations

import os

import pytest

from pare_hardware_mcp.devices import (DeviceError, ResolvedDevice,
                                       list_serial_devices, resolve_device)

TIGARD = "usb-SecuringHardware.com_Tigard_V1.1_TG1119e7-if01-port0"


@pytest.fixture
def fake_by_id(tmp_path):
    """A /dev/serial/by-id lookalike: symlinks pointing at stand-in ttys."""
    root = tmp_path / "by-id"
    root.mkdir()
    tty = tmp_path / "ttyUSB1"
    tty.write_text("")
    (root / TIGARD).symlink_to(tty)
    return root, tty


def test_resolving_a_by_id_path_reports_the_tty_and_the_serial(fake_by_id):
    root, tty = fake_by_id
    dev = resolve_device(str(root / TIGARD), expect_serial="TG1119e7")
    assert isinstance(dev, ResolvedDevice)
    assert dev.tty == str(tty)
    assert dev.serial == "TG1119e7"
    assert dev.interface == "if01"


def test_a_mismatched_serial_is_refused_and_names_both_values(fake_by_id):
    root, _ = fake_by_id
    with pytest.raises(DeviceError) as e:
        resolve_device(str(root / TIGARD), expect_serial="TG0000aa")
    assert "TG1119e7" in str(e.value) and "TG0000aa" in str(e.value)


def test_a_raw_tty_path_is_refused_rather_than_resolved(fake_by_id):
    _, tty = fake_by_id
    with pytest.raises(DeviceError) as e:
        resolve_device(str(tty), expect_serial=None)
    assert "by-id" in str(e.value)


def test_an_absent_path_names_the_path_and_does_not_scan_for_alternatives(tmp_path):
    missing = tmp_path / "by-id" / "usb-Nothing_Here-if00-port0"
    with pytest.raises(DeviceError) as e:
        resolve_device(str(missing), expect_serial=None)
    assert str(missing) in str(e.value)


def test_no_expected_serial_still_reports_what_was_found(fake_by_id):
    root, _ = fake_by_id
    dev = resolve_device(str(root / TIGARD), expect_serial=None)
    assert dev.serial == "TG1119e7"


def test_listing_reports_every_by_id_entry(fake_by_id):
    root, _ = fake_by_id
    found = list_serial_devices(str(root))
    assert [d.serial for d in found] == ["TG1119e7"]


def test_listing_an_absent_root_is_empty_not_an_error(tmp_path):
    # A bench with no adapter plugged in is a normal state, not a failure.
    assert list_serial_devices(str(tmp_path / "nope")) == []
