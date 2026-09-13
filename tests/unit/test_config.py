# tests/unit/test_config.py
from __future__ import annotations

import pytest

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


def test_request_deadline_defaults_to_sixty_seconds(monkeypatch):
    monkeypatch.delenv("PARE_HW_REQUEST_DEADLINE_S", raising=False)
    assert load_config().request_deadline_s == 60.0


def test_request_deadline_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PARE_HW_REQUEST_DEADLINE_S", "12.5")
    assert load_config().request_deadline_s == 12.5


def test_an_empty_request_deadline_env_var_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("PARE_HW_REQUEST_DEADLINE_S", "")
    assert load_config().request_deadline_s == 60.0


def test_a_non_numeric_request_deadline_is_a_clear_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("PARE_HW_REQUEST_DEADLINE_S", "soon")
    with pytest.raises(ValueError) as e:
        load_config()
    assert "soon" in str(e.value)


def test_a_non_positive_request_deadline_is_refused(monkeypatch):
    monkeypatch.setenv("PARE_HW_REQUEST_DEADLINE_S", "0")
    with pytest.raises(ValueError):
        load_config()


def test_scan_budget_defaults_to_the_baud_modules_budget(monkeypatch):
    """Asserted as "the config default IS the module's", not as 12.0.

    The number is calibration that lives in baud.py and is expected to move
    with the bench; a copy of it typed here would only record when the two
    last agreed.
    """
    from pare_hardware_mcp.baud import DEFAULT_SCAN_BUDGET_SECONDS
    monkeypatch.delenv("PARE_HW_SCAN_BUDGET_S", raising=False)
    assert load_config().scan_budget_s == DEFAULT_SCAN_BUDGET_SECONDS


def test_scan_budget_is_read_from_the_environment(monkeypatch):
    # The operator lever for a worker whose request deadline is below the
    # default budget: without it that worker cannot scan at all, because the
    # sweep's budget no longer shrinks when the caller passes fewer rates.
    monkeypatch.setenv("PARE_HW_SCAN_BUDGET_S", "4.5")
    assert load_config().scan_budget_s == 4.5


def test_an_empty_scan_budget_env_var_is_treated_as_unset(monkeypatch):
    from pare_hardware_mcp.baud import DEFAULT_SCAN_BUDGET_SECONDS
    monkeypatch.setenv("PARE_HW_SCAN_BUDGET_S", "")
    assert load_config().scan_budget_s == DEFAULT_SCAN_BUDGET_SECONDS


def test_a_non_positive_scan_budget_is_refused(monkeypatch):
    monkeypatch.setenv("PARE_HW_SCAN_BUDGET_S", "0")
    with pytest.raises(ValueError):
        load_config()


def test_a_non_numeric_scan_budget_is_a_clear_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("PARE_HW_SCAN_BUDGET_S", "twelve")
    with pytest.raises(ValueError) as e:
        load_config()
    assert "twelve" in str(e.value)


def test_artifact_root_defaults_to_none_when_unset(monkeypatch):
    monkeypatch.delenv("PARE_HW_ARTIFACT_ROOT", raising=False)
    assert load_config().artifact_root is None


def test_artifact_root_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", "/mnt/bench-store")
    assert load_config().artifact_root == "/mnt/bench-store"


def test_an_empty_artifact_root_env_var_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("PARE_HW_ARTIFACT_ROOT", "")
    assert load_config().artifact_root is None


def test_buffer_bytes_defaults_to_the_ringbuffer_default(monkeypatch):
    from pare_hardware_mcp.ringbuffer import DEFAULT_CAPACITY
    monkeypatch.delenv("PARE_HW_BUFFER_BYTES", raising=False)
    assert load_config().buffer_bytes == DEFAULT_CAPACITY


def test_buffer_bytes_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PARE_HW_BUFFER_BYTES", "1048576")
    assert load_config().buffer_bytes == 1048576


def test_an_empty_buffer_bytes_env_var_is_treated_as_unset(monkeypatch):
    from pare_hardware_mcp.ringbuffer import DEFAULT_CAPACITY
    monkeypatch.setenv("PARE_HW_BUFFER_BYTES", "")
    assert load_config().buffer_bytes == DEFAULT_CAPACITY


def test_a_non_numeric_buffer_bytes_is_a_clear_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("PARE_HW_BUFFER_BYTES", "lots")
    with pytest.raises(ValueError) as e:
        load_config()
    assert "lots" in str(e.value)


def test_a_non_positive_buffer_bytes_is_refused(monkeypatch):
    monkeypatch.setenv("PARE_HW_BUFFER_BYTES", "0")
    with pytest.raises(ValueError):
        load_config()
