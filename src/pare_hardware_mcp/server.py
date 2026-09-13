"""Bind the contract to handlers and serve.

The FastMCP instance is named for the DISTRIBUTION on purpose: stamp_version
resolves the version it reports in serverInfo from installed package metadata
keyed by this name, and for a networked worker serverInfo is the only provenance
the daemon ever sees. Rename one without the other and the wire quietly reports
no version.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pare_worker_kit import (PRODUCES_META_KEY, RISK_TIER_META_KEY, run_worker)

from pare_hardware_mcp import tools as tools_mod
from pare_hardware_mcp.contract import TOOL_SPECS


def build_server() -> FastMCP:
    server = FastMCP("pare-hardware-mcp")
    for spec in TOOL_SPECS:
        handler = getattr(tools_mod, spec.name)   # no stubs in phase 1
        server.add_tool(
            handler,
            name=spec.name,
            description=spec.description,
            meta={RISK_TIER_META_KEY: spec.risk_tier,
                  PRODUCES_META_KEY: spec.produces},
        )
    return server


def main() -> None:
    run_worker(build_server())
