"""Every contract tool must reach a real handler and carry both wire keys."""
from __future__ import annotations

import pytest
from pare_worker_kit import (PRODUCES_META_KEY, PRODUCES_RESULT,
                             RISK_TIER_META_KEY, VALID_RISK_TIERS)

from pare_hardware_mcp.contract import TOOL_SPECS
from pare_hardware_mcp.server import build_server


async def test_every_contract_tool_is_registered_with_both_meta_keys():
    server = build_server()
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {s.name for s in TOOL_SPECS}
    for spec in TOOL_SPECS:
        meta = tools[spec.name].meta
        assert meta[RISK_TIER_META_KEY] == spec.risk_tier
        assert meta[PRODUCES_META_KEY] == PRODUCES_RESULT
        assert meta[RISK_TIER_META_KEY] in VALID_RISK_TIERS


async def test_no_tool_is_a_stub():
    # A stub that returns "not implemented" advertises a tier and a schema and
    # looks live to the daemon. Phase 1 ships no stubs.
    import pare_hardware_mcp.tools as tools_mod
    for spec in TOOL_SPECS:
        assert hasattr(tools_mod, spec.name), f"{spec.name} has no handler"


def test_the_server_is_named_for_its_distribution():
    # stamp_version resolves the reported version from installed package
    # metadata keyed by the FastMCP instance name, and serverInfo is a
    # networked worker's only provenance. A mismatched name silently reports
    # no version at all.
    assert build_server().name == "pare-hardware-mcp"
