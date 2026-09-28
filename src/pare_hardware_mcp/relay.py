"""Relay power control for the bench target.

The relay is a DSD TECH SH-UR04A (CP2102 bridge, 9600 8N1), a 4-channel AT
relay: `AT+CHn=1` energises channel n, `AT+CHn=0` releases it, `AT+CHn=?`
queries it, `AT` alone replies `OK`. This module is the only place that
speaks that protocol -- the polarity math and every `AT+CHn=` string live
here so the MCP tools and the baud sweep only ever say "on"/"off"/"status".
See the design doc, spec sections 2.2/3/6/7
(`docs/superpowers/specs/2026-09-27-power-cycle-and-baud-design.md`).

`AT+BAUD=` is never sent by this module -- it would change the relay's own
serial rate, not the target's. That command does not appear anywhere below.

The relay is opened **per command**: a short 9600 session, send, read the
reply, close. It is not a capture device and there is no session to keep, so
a crashed worker cannot leave a relay handle claimed (see D6/D7 and section
2.1 of the design doc).

Real-hardware facts (from the 2026-09-27 spike), so the fake in
`tests/unit/test_relay.py` matches reality:
- `AT\\r\\n` -> `b'OK\\n\\x00'`
- `AT+CH1=1\\r\\n` -> `b'OK\\n\\x00'` (and `=0` likewise)
- `AT+CH1=?\\r\\n` -> `b'OK+CH1=0\\n\\x00'` (the digit is the channel state:
  `1` = energised, `0` = idle)
So the `OK`-prefix check tolerates a trailing `\\n\\x00`, and the query
parser reads the single digit after `=`.
"""
from __future__ import annotations

import time

import serial

RELAY_BAUD = 9600
"""The SH-UR04A's fixed serial rate -- unrelated to the target's UART baud."""

OFF_MS_FLOOR = 250
"""Below this the rail may not actually drop before power is restored."""

OFF_MS_CEILING = 30000
"""Above this a caller can strand the target off for an unreasonable time."""

_VALID_POLARITIES = ("nc", "no")
_VALID_STATES = ("on", "off")


class RelayError(RuntimeError):
    """Any failure between here and the relay device.

    Never a raw `serial.SerialException` or a silently-swallowed bad reply --
    both are converted here so a caller (or a language model) sees one
    exception family for "the relay didn't do what we asked."
    """


class RelayNotConfigured(RelayError):
    """Raised by `RelayController.from_config` when the relay is not set up.

    Any of `relay_device`, `relay_channel`, `relay_polarity` being absent
    means "no relay is declared for this bench" -- distinct from a relay
    that is declared but unreachable, which is a plain `RelayError`.
    """


def _energised_for_state(polarity: str, state: str) -> bool:
    """Whether the channel must be energised for the target to be `state`.

    Transcribed from the spec's polarity model (section 2.2), stated once
    for the implementer:
    - `nc`: target on <=> channel idle (`AT+CHn=0`); target off <=> channel
      energised (`AT+CHn=1`).
    - `no`: target on <=> channel energised (`AT+CHn=1`); target off <=>
      channel idle (`AT+CHn=0`).
    """
    on = state == "on"
    if polarity == "nc":
        return not on
    return on


def _clamp_off_ms(off_ms: int) -> int:
    return max(OFF_MS_FLOOR, min(OFF_MS_CEILING, off_ms))


class RelayController:
    """Drives one channel of a DSD TECH SH-UR04A over its AT protocol.

    `serial_factory` is the test seam: it is called with `device` and must
    return an object supporting `write`, `read_until`, `flush`,
    `reset_input_buffer` and `close` -- the same shape as `serial.Serial`.
    Production code leaves it unset and gets a real `serial.Serial`.
    """

    def __init__(self, device: str, channel: int, polarity: str, *,
                 serial_factory=None):
        if polarity not in _VALID_POLARITIES:
            raise RelayError(
                f"unknown relay polarity {polarity!r} (must be 'nc' or 'no')")
        self._device = device
        self._channel = channel
        self._polarity = polarity
        self._serial_factory = serial_factory or self._open_real_serial

    @staticmethod
    def _open_real_serial(device: str) -> serial.Serial:
        return serial.Serial(device, baudrate=RELAY_BAUD, timeout=2.0)

    @classmethod
    def from_config(cls, config) -> "RelayController":
        """Build a controller from `Config`, or raise if the relay is unset.

        Any one of `relay_device`/`relay_channel`/`relay_polarity` missing
        means the operator has not declared a relay for this bench -- not an
        error in the relay itself, so it is its own exception
        (`RelayNotConfigured`) rather than a plain `RelayError`.
        """
        if (config.relay_device is None or config.relay_channel is None
                or config.relay_polarity is None):
            raise RelayNotConfigured(
                "relay is not configured: set PARE_HW_RELAY_DEVICE, "
                "PARE_HW_RELAY_CHANNEL and PARE_HW_RELAY_POLARITY")
        return cls(config.relay_device, config.relay_channel,
                    config.relay_polarity)

    # -- device I/O -----------------------------------------------------

    def _open(self):
        try:
            return self._serial_factory(self._device)
        except serial.SerialException as exc:
            raise RelayError(
                f"could not open relay device {self._device}: {exc}") from exc

    def _at(self, port, cmd: str) -> str:
        """Send one AT command, return its reply with `OK` verified.

        Write discipline: `f"{cmd}\\r\\n"`. A reply that is empty, garbled,
        or that a serial-level failure prevents from arriving is a
        `RelayError` naming the command and (when there is a reply) the raw
        bytes -- never a silent success.
        """
        try:
            port.reset_input_buffer()
            port.write(f"{cmd}\r\n".encode("ascii"))
            port.flush()
            raw = port.read_until(b"\x00")
        except serial.SerialException as exc:
            raise RelayError(
                f"relay command {cmd!r} to {self._device} failed: {exc}"
            ) from exc
        text = raw.decode("ascii", errors="replace").strip("\x00").strip()
        if not text.startswith("OK"):
            raise RelayError(
                f"relay command {cmd!r} to {self._device} got an "
                f"unexpected reply: {raw!r}")
        return text

    def _run(self, cmd: str) -> str:
        port = self._open()
        try:
            return self._at(port, cmd)
        finally:
            port.close()

    # -- public API -------------------------------------------------------

    def _status_from_energised(self, energised: bool) -> dict:
        target_on = (not energised) if self._polarity == "nc" else energised
        return {
            "target_power": "on" if target_on else "off",
            "channel_state": "energised" if energised else "idle",
            "channel": self._channel,
            "polarity": self._polarity,
        }

    def status(self) -> dict:
        reply = self._run(f"AT+CH{self._channel}=?")
        # reply is "OK+CHn=<0|1>"; the digit is the last character.
        digit = reply.rsplit("=", 1)[-1].strip()[:1]
        return self._status_from_energised(digit == "1")

    def set_power(self, state: str) -> dict:
        """Set the target's power and return the resulting status.

        Returns the status derived from the state just written, without a
        second device round-trip: a fresh `AT+CHn=?` query after every
        `AT+CHn=` write would double the relay traffic for no information
        this driver doesn't already have (we just told the device what to
        set; a caller wanting a live re-check can call `status()` itself).
        """
        if state not in _VALID_STATES:
            raise RelayError(
                f"invalid power state {state!r} (must be 'on' or 'off')")
        energised = _energised_for_state(self._polarity, state)
        self._run(f"AT+CH{self._channel}={1 if energised else 0}")
        return self._status_from_energised(energised)

    def power_cycle(self, off_ms: int) -> dict:
        """Off, wait `off_ms` (clamped), on -- restoring on in a `finally`.

        The restore ALWAYS runs, even if the off-write or the sleep raises,
        so an exception mid-cycle still leaves the target powered rather
        than stranded off (D7). `off_ms_actual` is the clamped value that
        was actually waited, so a caller can tell when its request was
        adjusted.
        """
        clamped_ms = _clamp_off_ms(off_ms)
        try:
            self.set_power("off")
            time.sleep(clamped_ms / 1000.0)
        finally:
            self.set_power("on")
        return {"status": self.status(), "off_ms_actual": clamped_ms}
