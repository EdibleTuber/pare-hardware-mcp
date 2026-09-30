# tests/unit/test_deploy_manifest.py
"""Guard: the deploy manifest must name every source module.

scripts/bench_deploy.sh installs an EXPLICIT file list into
/opt/pare/hardware-mcp and then `pip install -e` that tree, so a module
missing from the manifest is not "not deployed" -- it is an ImportError on
worker startup, because the running package IS the deployed tree
(ReadOnlyPaths=/opt/pare/hardware-mcp in the unit). This guard exists
because the manifest went stale exactly once already: relay.py (added by
the power-cycle-and-baud branch) was absent from FILES, and deploying that
branch as-is would have shipped a worker that cannot start.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench_deploy.sh"
PKG = ROOT / "src" / "pare_hardware_mcp"


def _manifested_paths() -> set[str]:
    """The repo-side paths in the FILES=( ... ) array of the deploy script."""
    text = SCRIPT.read_text()
    out: set[str] = set()
    in_array = False
    for line in text.splitlines():
        if line.startswith("FILES=("):
            in_array = True
            continue
        if not in_array:
            continue
        if line.strip() == ")":
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.add(stripped.strip('"').split(":", 1)[0])
    return out


def test_deploy_manifest_covers_every_source_module():
    scripted = _manifested_paths()
    missing = [
        p.name
        for p in sorted(PKG.glob("*.py"))
        if f"src/pare_hardware_mcp/{p.name}" not in scripted
    ]
    assert not missing, (
        f"scripts/bench_deploy.sh does not deploy {missing} -- the worker "
        "would ImportError on startup (the editable install reads the "
        "deployed tree, and the unit pins it read-only)"
    )
