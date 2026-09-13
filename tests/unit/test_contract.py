"""The contract is the one module PARE's CI imports on a runner with no Pi
attached, so it must not reach hardware at import time."""
from __future__ import annotations

import subprocess
import sys

from pare_worker_kit import PRODUCES_RESULT, VALID_RISK_TIERS

from pare_hardware_mcp.contract import CONTRACT_VERSION, TOOL_SPECS

EXPECTED = {
    "list_devices", "bench_status", "console_detect_baud", "console_open",
    "console_read", "console_send", "console_status", "console_close",
}


def test_the_contract_declares_exactly_the_phase_1_surface():
    assert {s.name for s in TOOL_SPECS} == EXPECTED


def test_no_tool_name_carries_the_worker_prefix():
    # tool_factory prefixes with the workers.yaml key, so a name written
    # `hardware_console_send` here dispatches as hardware_hardware_console_send
    # and the operator pin silently matches nothing.
    for spec in TOOL_SPECS:
        assert not spec.name.startswith("hardware_"), spec.name


def test_every_tool_advertises_a_valid_tier_and_produces_result():
    for spec in TOOL_SPECS:
        assert spec.risk_tier in VALID_RISK_TIERS, spec.name
        assert spec.produces == PRODUCES_RESULT, spec.name


def test_send_is_high_and_open_is_medium_and_the_rest_are_low():
    tiers = {s.name: s.risk_tier for s in TOOL_SPECS}
    assert tiers["console_send"] == "high"
    assert tiers["console_open"] == "medium"
    for name in EXPECTED - {"console_send", "console_open"}:
        assert tiers[name] == "low", name


def test_the_contract_imports_without_pyserial():
    # PARE's CI imports this on a runner to validate risk pins. If the contract
    # pulls in pyserial (or anything that opens a device) that import fails and
    # the pin check silently degrades to "unchecked".
    code = (
        "import sys; sys.modules['serial'] = None;"
        "import pare_hardware_mcp.contract as c; print(len(c.TOOL_SPECS))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == len(EXPECTED)


def test_contract_version_is_declared():
    assert CONTRACT_VERSION >= 1


def test_console_read_states_its_payload_bound_in_the_description():
    # A model that cannot see the ceiling asks for the whole buffer and gets a
    # silently truncated answer. Asserted against the constants tools.py
    # enforces, not against literals, so the two cannot drift apart.
    from pare_hardware_mcp.ringbuffer import DEFAULT_READ_LIMIT, MAX_READ_LIMIT
    spec = next(s for s in TOOL_SPECS if s.name == "console_read")
    assert str(DEFAULT_READ_LIMIT) in spec.description
    assert str(MAX_READ_LIMIT) in spec.description
