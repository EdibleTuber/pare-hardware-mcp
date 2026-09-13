# pare-hardware-mcp

An MCP worker exposing a hardware bench's UART console to AI agents via the Model Context Protocol.

## What it is

This worker runs on the machine where a Tigard hardware debugger is physically attached. It exposes eight tools for managing the UART console session: device listing, baud-rate detection, opening/closing the console, reading captured output, and sending commands to the target.

## Dependencies

- `mcp >= 1.27.0, < 2`
- `pare-worker-kit >= 0.1.2` (git dependency)
- `pyserial >= 3.5`
- Python >= 3.12

## Deployment

This worker is meant to run on the Raspberry Pi (or similar) that the hardware bench is physically wired to. It communicates with the PARE daemon via MCP over stdio.

## Development

```bash
pip install -e ".[dev]"
pytest
```
