"""The PARE hardware-bench worker: a Tigard's UART console, over MCP.

Runs on the machine the hardware is wired to. Depends on `mcp`,
`pare-worker-kit` and `pyserial` -- never `agent_core`, which is the client
side and would drag a daemon's dependency tree onto a Raspberry Pi.
"""
__version__ = "0.1.0"
