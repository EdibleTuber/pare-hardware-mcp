# tests/unit/test_tools_capture.py
"""`console_capture` handler: snapshot semantics and the descriptor contract.

No hardware: the session is a `FakeSession` carrying a real `CaptureBuffer`
(the pattern of test_tools_read.py), and the artifact root is a tmpdir
holding a `.bench-store-id` sentinel -- open_artifact requires the sentinel,
not a mount (parent spec §5 step 2).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from pare_hardware_mcp import tools
from pare_hardware_mcp.config import Config
from pare_hardware_mcp.ringbuffer import CaptureBuffer

from pare_worker_kit import PRODUCES_ARTIFACT, RESERVED_SLUG_ARG, RESERVED_DRIVE_ID_ARG

SENTINEL = "b2657680-9f16-4918-9195-ef3ba17da924"
SLUG = "wbench-test"  # must satisfy the kit's SLUG_RE -- check at run time


def reserved():
    """The two daemon-injected arguments, at their wire names (ledger)."""
    return {RESERVED_SLUG_ARG: SLUG, RESERVED_DRIVE_ID_ARG: SENTINEL}


class FakeSession:
    id = "s-1"
    alive = True

    def __init__(self, capacity=4096):
        self.buffer = CaptureBuffer(capacity=capacity)


class FakeManager:
    def __init__(self, capacity=4096):
        self.current = FakeSession(capacity=capacity)

    def get(self, session_id):
        if session_id != self.current.id:
            raise KeyError(session_id)
        return self.current


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A session manager plus an artifact root with the sentinel. No device."""
    root = tmp_path / "bench-store"
    root.mkdir()
    (root / ".bench-store-id").write_text(SENTINEL + "\n")  # bench/INSTALL.md:194 recipe
    monkeypatch.setattr(tools, "CONFIG", Config(artifact_root=str(root)))
    mgr = FakeManager()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    return mgr, root


async def test_capture_writes_the_window_and_returns_only_the_descriptor(rig):
    mgr, root = rig
    payload = b"boot log: 12345" * 100
    mgr.current.buffer.append(payload)
    out = json.loads(await tools.console_capture(session="s-1", name="boot-log",
                                                 from_cursor=0, **reserved()))
    # Exactly the seven worker-reported descriptor fields (spec §4 invariant 4) -- no extras.
    assert set(out) == {"path", "size", "sha256", "hashed_at", "media_type",
                        "drive_id", "host"}
    on_disk = root / SLUG / "boot-log"
    assert on_disk.read_bytes() == payload          # the bytes read are the bytes written
    assert out["size"] == len(payload)
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()
    assert out["drive_id"] == SENTINEL
    assert out["media_type"] == "application/octet-stream"


async def test_to_cursor_defaults_to_the_head_sampled_at_handler_start(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"first")
    await tools.console_capture(session="s-1", name="a", **reserved())
    assert (root / SLUG / "a").read_bytes() == b"first"


async def test_capture_honours_an_explicit_window(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"0123456789")
    await tools.console_capture(session="s-1", name="w",
                                from_cursor=2, to_cursor=7, **reserved())
    assert (root / SLUG / "w").read_bytes() == b"23456"


async def test_capture_does_not_consume_the_buffer(rig):
    # Snapshot, not drain (spec §4 invariant 1): the next read still sees everything.
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    await tools.console_capture(session="s-1", name="a", **reserved())
    mgr.current.buffer.append(b"efgh")
    data, next_cursor, dropped, _remaining = mgr.current.buffer.read(0, None)
    assert data == b"abcdefgh" and next_cursor == 8 and dropped == 0


async def test_handler_result_validates_under_the_daemon_validator(rig):
    # The parent §8 discipline: assert the RELATIONSHIP -- a descriptor the kit
    # produces validates under agent_core's validator. No field-count assertion:
    # a literal breaks on the next legitimate change and a relationship survives it.
    from agent_core.workers.artifacts import validate_descriptor
    from agent_core.workers.types import WorkerSpec
    mgr, root = rig
    mgr.current.buffer.append(b"payload")
    out = json.loads(await tools.console_capture(session="s-1", name="rel", **reserved()))
    spec = WorkerSpec(name="hardware", transport="streamable_http",
                      endpoint="http://100.97.133.126:9102/mcp", risk_default="high",
                      artifact_root=str(root), artifact_drive_id=SENTINEL,
                      artifact_host="100.97.133.126")
    validate_descriptor(out, spec=spec, tool="console_capture", slug=SLUG)   # no exception IS the assertion


async def test_a_mismatched_drive_id_fails_validation(rig):
    # The negative half: the relationship must be able to FAIL. If the landed
    # validator does not reject a drive_id mismatch, STOP and surface it --
    # do not quietly swap in a different field.
    from agent_core.workers.artifacts import validate_descriptor, DescriptorError
    from agent_core.workers.types import WorkerSpec
    mgr, root = rig
    mgr.current.buffer.append(b"payload")
    out = json.loads(await tools.console_capture(session="s-1", name="rel", **reserved()))
    spec = WorkerSpec(name="hardware", transport="streamable_http",
                      endpoint="http://100.97.133.126:9102/mcp", risk_default="high",
                      artifact_root=str(root),
                      artifact_drive_id="0361c41f-e680-4d4e-b9c3-39af8a33d067",
                      artifact_host="100.97.133.126")
    with pytest.raises(DescriptorError):
        validate_descriptor(out, spec=spec, tool="console_capture", slug=SLUG)
