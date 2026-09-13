# tests/unit/test_config.py
from __future__ import annotations

from pare_hardware_mcp.config import Config, load_config


def test_defaults_are_none_when_unset(monkeypatch):
    monkeypatch.delenv("PARE_HW_DEVICE", raising=False)
    monkeypatch.delenv("PARE_HW_EXPECT_SERIAL", raising=False)
    cfg = load_config()
    assert cfg.device is None
    assert cfg.expect_serial is None


def test_reads_device_and_serial_from_the_environment(monkeypatch):
    monkeypatch.setenv("PARE_HW_DEVICE",
                       "/dev/serial/by-id/usb-FTDI_TTL232R-3V3_FT123-if00-port0")
    monkeypatch.setenv("PARE_HW_EXPECT_SERIAL", "FT123")
    cfg = load_config()
    assert cfg.device == "/dev/serial/by-id/usb-FTDI_TTL232R-3V3_FT123-if00-port0"
    assert cfg.expect_serial == "FT123"


def test_an_empty_string_env_var_is_treated_as_unset(monkeypatch):
    # A systemd unit with Environment=PARE_HW_DEVICE= (blank) must not resolve
    # to a truthy empty path -- that would fail deep inside device resolution
    # instead of at config load.
    monkeypatch.setenv("PARE_HW_DEVICE", "")
    monkeypatch.setenv("PARE_HW_EXPECT_SERIAL", "")
    cfg = load_config()
    assert cfg.device is None
    assert cfg.expect_serial is None


def test_config_is_frozen():
    cfg = Config()
    try:
        cfg.device = "/dev/x"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Config must be immutable")
