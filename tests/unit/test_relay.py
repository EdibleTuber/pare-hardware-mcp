# tests/unit/test_relay.py
"""RelayController against a fake serial port -- no real relay is touched.

The relay is a DSD TECH SH-UR04A (see the design doc, spec section 2.2/7):
AT protocol, `AT+CHn=1` energises channel n, `AT+CHn=0` releases it,
`AT+CHn=?` queries it, `AT` alone replies `OK`. The real device's replies
(from the spike) all carry a trailing `\n\x00` -- `FakeRelaySerial` matches
that shape so the OK-prefix check is exercised against real bytes, not a
tidier fake.
"""
from __future__ import annotations

import pytest
import serial

from pare_hardware_mcp.config import Config
from pare_hardware_mcp.relay import (OFF_MS_CEILING, OFF_MS_FLOOR,
                                      RelayController, RelayError,
                                      RelayNotConfigured)


class FakeRelaySerial:
    """Records the last write, replays a scripted reply for it.

    Modeled on the brief: `write` records the command, the read side
    returns the reply scripted for the last-written command (or a default
    `OK\\n\\x00` success), and `flush`/`reset_input_buffer`/`close` are
    no-ops. `fail_after` lets a test inject a failure on a later call to
    pin the power_cycle `finally` (Step 9).
    """

    def __init__(self, replies: dict[str, bytes] | None = None,
                 fail_after: int | None = None):
        self.replies = dict(replies or {})
        self.last_write: str | None = None
        self.writes: list[str] = []
        self.closed = False
        self._fail_after = fail_after
        self._call_count = 0

    def write(self, data: bytes) -> None:
        self._call_count += 1
        if self._fail_after is not None and self._call_count == self._fail_after:
            raise serial.SerialException("simulated failure")
        text = data.decode()
        self.last_write = text
        self.writes.append(text)

    def read_until(self, expected: bytes = b"\n", size=None) -> bytes:
        return self.replies.get(self.last_write, b"OK\n\x00")

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


# -- Step 1: nc polarity -----------------------------------------------------

def test_nc_polarity_maps_power_to_channel():
    fake = FakeRelaySerial(replies={
        "AT+CH1=0\r\n": b"OK\n\x00",     # set on -> idle for nc
        "AT+CH1=1\r\n": b"OK\n\x00",     # set off -> energised for nc
        "AT+CH1=?\r\n": b"OK+CH1=0\n\x00",
    })
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    r.set_power("on")
    assert fake.last_write == "AT+CH1=0\r\n"      # nc on = idle
    r.set_power("off")
    assert fake.last_write == "AT+CH1=1\r\n"      # nc off = energised
    st = r.status()                                # channel idle -> nc target on
    assert st["target_power"] == "on"
    assert st["channel_state"] == "idle"
    assert st["channel"] == 1
    assert st["polarity"] == "nc"


# -- Step 5: no polarity is not implied --------------------------------------

def test_no_polarity_maps_power_to_channel():
    fake = FakeRelaySerial(replies={
        "AT+CH1=1\r\n": b"OK\n\x00",     # set on -> energised for no
        "AT+CH1=0\r\n": b"OK\n\x00",     # set off -> idle for no
        "AT+CH1=?\r\n": b"OK+CH1=1\n\x00",
    })
    r = RelayController("/dev/relay", 1, "no", serial_factory=lambda dev: fake)
    r.set_power("on")
    assert fake.last_write == "AT+CH1=1\r\n"      # no on = energised
    r.set_power("off")
    assert fake.last_write == "AT+CH1=0\r\n"      # no off = idle
    st = r.status()                                # channel energised -> no target on
    assert st["target_power"] == "on"
    assert st["channel_state"] == "energised"


# -- Step 6: garbage reply is an error, not a silent success -----------------

def test_garbage_reply_is_an_error_not_a_silent_success():
    fake = FakeRelaySerial(replies={"AT+CH1=1\r\n": b"\x00\xff garbage"})
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError):
        r.set_power("off")


# -- Fix round 1, Finding 1: safe_message never carries the wire protocol ---

def test_bad_reply_safe_message_omits_the_at_command_but_keeps_the_reason():
    fake = FakeRelaySerial(replies={"AT+CH1=1\r\n": b""})
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError) as e:
        r.set_power("off")
    # str(exc) is the full diagnostic and MAY name the raw command --
    # that's the log line, never the tool reply.
    assert "AT+CH1=1" in str(e.value)
    # safe_message is what a handler must return: same reason, no protocol.
    assert "AT+CH" not in e.value.safe_message
    assert "/dev/relay" in e.value.safe_message
    assert "no ok reply" in e.value.safe_message.lower()


def test_serial_failure_safe_message_omits_the_at_command_but_keeps_the_reason():
    fake = FakeRelaySerial()

    def failing_write(data):
        raise serial.SerialException("timeout")
    fake.write = failing_write
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError) as e:
        r.set_power("off")
    assert "AT+CH1=1" in str(e.value)
    assert "AT+CH" not in e.value.safe_message
    assert "timeout" in e.value.safe_message
    assert "/dev/relay" in e.value.safe_message


def test_empty_reply_is_an_error_not_a_silent_success():
    fake = FakeRelaySerial(replies={"AT+CH1=1\r\n": b""})
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError):
        r.set_power("off")


# -- Step 7: device open failure is a clean RelayError -----------------------

def test_open_failure_is_a_relay_error_naming_the_device():
    def factory(dev):
        raise serial.SerialException("No such file or directory")

    r = RelayController("/dev/relay-missing", 1, "nc", serial_factory=factory)
    with pytest.raises(RelayError) as e:
        r.set_power("on")
    assert "/dev/relay-missing" in str(e.value)

    with pytest.raises(RelayError) as e:
        r.status()
    assert "/dev/relay-missing" in str(e.value)


# -- Step 8: off_ms bounds ----------------------------------------------------

def test_power_cycle_clamps_off_ms(monkeypatch):
    # The clamped value is what power_cycle reports and what it sleeps for;
    # the sleep itself is not under test here (that would make this test
    # wait out a real 30s ceiling), so time.sleep is stubbed to a no-op.
    monkeypatch.setattr("pare_hardware_mcp.relay.time.sleep", lambda s: None)
    fake = FakeRelaySerial()
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    assert r.power_cycle(0)["off_ms_actual"] == OFF_MS_FLOOR
    assert r.power_cycle(10**9)["off_ms_actual"] == OFF_MS_CEILING
    assert r.power_cycle(1000)["off_ms_actual"] == 1000


# -- Step 9: resting state is restored even on failure -----------------------

def test_power_cycle_restores_power_when_the_off_write_itself_fails(monkeypatch):
    # fail_after=1: the very first write (the off-set) raises. The `finally`
    # must still attempt the on-set, and it must land -- restoring power even
    # when the off command never reached the relay at all.
    fake = FakeRelaySerial(fail_after=1)
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)
    with pytest.raises(RelayError):
        r.power_cycle(250)
    assert fake.writes[-1] == "AT+CH1=0\r\n"  # restore-to-on landed (nc on = idle)


def test_power_cycle_restores_power_after_the_off_write_succeeds(monkeypatch):
    # The off write itself succeeds; the failure is injected in the very
    # next step (the off-window sleep) -- "a failure right after the off
    # write succeeds." The `finally` must still restore power despite it.
    fake = FakeRelaySerial()
    r = RelayController("/dev/relay", 1, "nc", serial_factory=lambda dev: fake)

    def boom(seconds):
        raise RuntimeError("simulated failure during the off window")

    monkeypatch.setattr("pare_hardware_mcp.relay.time.sleep", boom)
    with pytest.raises(RuntimeError):
        r.power_cycle(250)
    assert fake.writes == ["AT+CH1=1\r\n", "AT+CH1=0\r\n"]  # off, then restore-to-on


# -- Step 10: RelayNotConfigured ---------------------------------------------

def test_from_config_raises_when_any_field_is_absent():
    with pytest.raises(RelayNotConfigured):
        RelayController.from_config(Config())


def test_from_config_builds_a_controller_when_fully_configured():
    cfg = Config(relay_device="/dev/relay", relay_channel=2, relay_polarity="no")
    r = RelayController.from_config(cfg)
    assert isinstance(r, RelayController)
