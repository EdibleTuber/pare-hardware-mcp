# tests/unit/test_tools_write.py
from __future__ import annotations

import base64
import inspect
import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.config import Config
from pare_hardware_mcp.session import SessionError


# -- console_send -----------------------------------------------------------

async def test_send_writes_exactly_the_decoded_bytes(monkeypatch):
    written = []

    class FakeSession:
        id, alive = "s-1", True
        def write(self, data): written.append(data)

    class FakeManager:
        current = FakeSession()
        def get(self, sid):
            if sid != "s-1": raise KeyError(sid)
            return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    payload = base64.b64encode(b"\x03reboot\r").decode()
    out = json.loads(await tools.console_send(session="s-1", data_b64=payload))
    assert written == [b"\x03reboot\r"]
    assert out["sent"] == 8


async def test_send_does_not_fabricate_capture_entries(monkeypatch):
    # The capture is what the TARGET said. Echoing our own bytes into it would
    # make the log assert something the target never emitted.
    from pare_hardware_mcp.ringbuffer import CaptureBuffer

    class FakeSession:
        id, alive = "s-1", True
        def __init__(self): self.buffer = CaptureBuffer(capacity=256)
        def write(self, data): pass

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    mgr = FakeManager()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    await tools.console_send(session="s-1", data_b64=base64.b64encode(b"hi").decode())
    assert mgr.current.buffer.head == 0


async def test_send_to_a_dead_session_is_refused_with_the_reason(monkeypatch):
    class FakeSession:
        id, alive, death_reason = "s-1", False, "device /dev/serial/by-id/... vanished"
        def write(self, data): raise AssertionError("must not write")

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="aGk="))
    assert out["error"]
    assert "vanished" in out["error"]


async def test_malformed_base64_is_an_error_not_a_partial_write(monkeypatch):
    class FakeManager:
        def get(self, sid): raise AssertionError("must not reach the session")
    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="not!base64"))
    assert out["error"]


async def test_send_write_timeout_is_reported_not_raised(monkeypatch):
    # ConsoleSession.write() raises SessionError on a bounded write timeout.
    # This does NOT kill the session -- it's the target not asserting CTS --
    # so the handler must catch it and hand back the error contract, not let
    # it escape as an opaque transport failure.
    class FakeSession:
        id, alive = "s-1", True
        def write(self, data):
            raise SessionError("write on session s-1 timed out after 5.0s: "
                                "the target is not accepting bytes (flow=rtscts)")

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="aGk="))
    assert out["error"]
    assert "timed out" in out["error"]
    assert "sent" not in out


async def test_send_short_write_preserves_the_lower_bound_wording(monkeypatch):
    # pyserial's abort path drops the final chunk from its count, so the
    # reported count is a LOWER bound ("at least N of M"), never an exact
    # boundary. That wording must survive to the caller intact, and `sent`
    # must not be reported since the write did not demonstrably complete.
    msg = ("write on session s-1 was aborted after at least 64000 of 73728 "
           "bytes (the session is being closed). The exact boundary is not "
           "knowable -- pyserial's abort drops the last chunk from its "
           "count -- so more than 64000 bytes may have reached the target. "
           "Assume the command did not take effect, and do not resume from "
           "this offset.")

    class FakeSession:
        id, alive = "s-1", True
        def write(self, data): raise SessionError(msg)

    class FakeManager:
        def __init__(self): self.current = FakeSession()
        def get(self, sid): return self.current

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_send(session="s-1", data_b64="aGk="))
    assert out["error"] == msg
    assert "at least 64000" in out["error"]
    assert "sent" not in out


# -- console_open -------------------------------------------------------

def test_console_open_signature_has_no_expect_serial_parameter():
    # A caller-supplied expect_serial would defeat the whole point of the
    # check: an operator declares the expected serial out of band, not the
    # model calling the tool.
    params = inspect.signature(tools.console_open).parameters
    assert "expect_serial" not in params
    assert "device" in params
    # device must be optional so PARE_HW_DEVICE can supply it.
    assert params["device"].default is None


async def test_console_open_falls_back_to_config_device(monkeypatch):
    seen = {}

    class FakeSession:
        id = "s-1"
        class device:
            by_id = "/dev/serial/by-id/from-config"
            serial = "ABC123"
        baud = 115200
        flow = "none"
        dtr = False
        rts = False
        class buffer:
            head = 0

    class FakeManager:
        def open(self, **kwargs):
            seen.update(kwargs)
            return FakeSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG",
                        Config(device="/dev/serial/by-id/from-config",
                               expect_serial=None))
    out = json.loads(await tools.console_open())
    assert seen["device"] == "/dev/serial/by-id/from-config"
    assert out["device"] == "/dev/serial/by-id/from-config"


async def test_console_open_always_uses_configured_expect_serial(monkeypatch):
    # Not a caller-supplied value under any circumstance -- console_open has
    # no parameter for it at all (see the signature test above). The manager
    # must always be called with the operator-declared value from Config.
    seen = {}

    class FakeSession:
        id = "s-1"
        class device:
            by_id = "/dev/x"
            serial = "SN-9"
        baud = 9600
        flow = "none"
        dtr = False
        rts = False
        class buffer:
            head = 0

    class FakeManager:
        def open(self, **kwargs):
            seen.update(kwargs)
            return FakeSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial="SN-9"))
    await tools.console_open()
    assert seen["expect_serial"] == "SN-9"


async def test_console_open_without_device_or_config_is_a_clean_error(monkeypatch):
    class FakeManager:
        def open(self, **kwargs): raise AssertionError("must not reach the manager")

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device=None, expect_serial=None))
    out = json.loads(await tools.console_open())
    assert out["error"]
    assert "PARE_HW_DEVICE" in out["error"]


async def test_console_open_reports_none_rts_as_json_null_not_false(monkeypatch):
    # Under flow="rtscts" pyserial never applies a requested RTS, so the
    # session reports rts=None. console_open's result must surface that
    # honestly as JSON null -- coercing it to false would be a false claim
    # about a line that resets target boards.
    class FakeSession:
        id = "s-1"
        class device:
            by_id = "/dev/x"
            serial = "SN-9"
        baud = 115200
        flow = "rtscts"
        dtr = False
        rts = None
        class buffer:
            head = 0

    class FakeManager:
        def open(self, **kwargs): return FakeSession()

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial=None))
    raw = await tools.console_open(flow="rtscts")
    assert '"rts": null' in raw
    out = json.loads(raw)
    assert out["rts"] is None


async def test_console_open_surfaces_manager_errors(monkeypatch):
    class FakeManager:
        def open(self, **kwargs): raise Exception("serial mismatch on /dev/x")

    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    monkeypatch.setattr(tools, "CONFIG", Config(device="/dev/x", expect_serial="Y"))
    out = json.loads(await tools.console_open())
    assert out["error"]
    assert "serial mismatch" in out["error"]
