# Console capture artifact — implementation plan

**Date:** 2026-10-02
**Spec:** `docs/superpowers/specs/2026-10-01-console-capture-artifact-design.md` (approved)
**Parent spec:** PARE `docs/superpowers/specs/2026-09-12-artifact-wiring-design.md`
**Status:** planned, **not started**. Execution is hard-gated (below) and happens in a fresh
session once the gate lifts.

## The gate — check before anything else

Nothing in this plan can execute until the parent spec's phases have landed. **Task 1 is the
gate check.** If any of these is false, stop and tell the operator — the next build is then the
parent's P1 plan, not this one:

1. `pare-worker-kit` has a **v0.3.0** tag whose `artifacts.py` contains
   `open_artifact(root, slug, name, *, media_type, expect_drive_id, expected_size)` (parent §5),
   the two **reserved-argument name constants** (parent A4/P1), and the named error
   exception(s) for its walk's failures.
2. `agent_core` has a release (v1.12.0 or whatever the parent's P1 names it) whose
   `validate_descriptor` takes the new signature (parent A3: `spec` + `tool`, not `worker: str`),
   with dispatch wiring for `produces=artifact` (P3) and the reserved-argument injection.
3. PARE's `pyproject.toml` pins that agent_core release (parent P4's pin bump).

## Placeholders — resolved by Task 1, recorded in the ledger

Upstream names that this plan cannot know yet are written `⟨placeholder⟩`. Task 1 reads the
**landed source** (never memory, never the parent spec's prose) and records each resolution in
the executing-plans ledger. Every later task reads them from the ledger.

| Placeholder | Resolves to | Source |
|---|---|---|
| `⟨SLUG_ARG_CONST⟩` / `⟨DRIVE_ARG_CONST⟩` | the kit's Python constant names holding the two reserved-argument **wire names** | `pare_worker_kit` v0.3.0, `artifacts.py` (or where P1 put them) |
| `⟨slug_param⟩` / `⟨drive_param⟩` | the wire-name strings themselves, used as the handler's parameter names — **must be valid Python identifiers**; if either is not, stop and surface it | values of the two constants |
| `⟨ARTIFACT_ERROR⟩` | the exception class(es) `open_artifact` raises for its named failures (sentinel absent/mismatch, EROFS, ENOSPC, EFBIG, stale `+partial`, EEXIST, short-write) | kit v0.3.0 |
| `⟨DAEMON_ARTIFACT_ERROR⟩` | agent_core's validator exception (today `ArtifactError`) | agent_core release |
| `⟨WORKER_TOOL_SPEC⟩` | the type `validate_descriptor`'s `tool` parameter takes (today `pare/tools.py:29`) | agent_core release |
| `⟨AGENT_CORE_TAG⟩` | the agent_core release tag, for the dev-dependency pin | agent_core `git tag` |
| `⟨ENOSPC_MSG⟩` | the kit's exact ENOSPC message, taken from the kit's source or its P2 test that pins it — **never invented here** | kit v0.3.0 |

If `open_artifact` is not exported from `pare_worker_kit`'s top level, import it from wherever
P2 put it and record the actual import line in the ledger.

## Global conventions

- **Test runners:** this repo `.venv/bin/python -m pytest`; the PARE repo `.venv/bin/python -m
  pytest` (never `uv run`). The venvs already exist.
- **Commits:** one per task, imperative subject. Hardware-repo tasks commit in
  `/mnt/secondary/projects/pare-hardware-mcp`; Task 7 commits in `/mnt/secondary/projects/PARE`.
- **RED before GREEN:** every new test is run against the pre-change code first, and the failure
  must be the one the test is meant to discriminate (spec §8).
- **No-hardware rule:** `contract.py` must keep importing without pyserial
  (`tests/unit/test_contract.py::test_the_contract_imports_without_pyserial` enforces it via
  `sys.modules['serial'] = None`).
- **The handler mirrors `console_read`** (`tools.py:152-201`): `MANAGER` calls off the event
  loop, buffer reads inline (leaf lock, memory copy), the disk write off the loop, every failure
  a named `{"error": …}` via `_err`.

## File map

| File | Task | Change |
|---|---|---|
| `pyproject.toml` | 1, 3 | kit pin v0.2.0 → v0.3.0; `dev` extra gains agent_core (test-only) |
| `src/pare_hardware_mcp/contract.py` | 2 | `console_capture` entry, `produces=PRODUCES_ARTIFACT` |
| `src/pare_hardware_mcp/tools.py` | 3, 4, 5 | `console_capture` handler (after `console_read`, before `console_status`) |
| `src/pare_hardware_mcp/config.py` | 6 | `artifact_root` docstring names its consumer |
| `systemd/pare-hardware-mcp.service` | 6 | `PARE_HW_ARTIFACT_ROOT` env + `ReadWritePaths` |
| `tests/unit/test_contract.py` | 2 | surface set, produces, reserved names |
| `tests/integration/test_wire_contract.py` | 2 | per-spec `produces`, wire reserved names |
| `tests/unit/test_tools_capture.py` | 3, 4, 5 | **new** — handler tests, tmpdir + sentinel rig |
| PARE `workers.yaml` | 7 | hardware entry's `artifact_root` / `artifact_drive_id` |
| PARE `tests/test_workers_yaml.py` | 7 | declaration test |

---

## Task 1 — Gate check + kit pin bump

**Files:** `pyproject.toml` (line 13).

**Context:** the whole plan is written against interfaces that do not exist yet. This task
resolves the `⟨placeholders⟩` from the landed source and moves the pin so the rest of the plan
compiles against reality.

Steps:

1. **Verify the gate** (read, don't recall):
   - `cd /mnt/secondary/projects/pare-worker-kit && git tag` — v0.3.0 exists.
   - Read its `artifacts.py` (at the tag): `open_artifact`'s signature matches parent §5; find
     the two reserved-argument constants, the exception class(es), and the ENOSPC message
     construction.
   - `cd /mnt/secondary/projects/agent_core && git tag && git log --oneline -5` — find the
     artifact release; read `workers/artifacts.py` (`validate_descriptor` signature) and the
     dispatch path for `produces=artifact`.
   - `grep agent_core /mnt/secondary/projects/PARE/pyproject.toml` — PARE pins that release.
   - **Record in the ledger:** every `⟨placeholder⟩` resolution, the exact import line for
     `open_artifact`, and the exact reserved-name strings.
   - Check both reserved-name strings are valid Python identifiers
     (`python3 -c "import keyword, ast; ast.parse('def f(⟨slug⟩, ⟨drive⟩): ...')"` style).
     If not: **stop** and surface — do not invent a mapping layer.
   - Any gate item false → **stop**, report which parent phase is missing.
2. Edit `pyproject.toml:13`:
   `pare-worker-kit @ git+https://github.com/EdibleTuber/pare-worker-kit.git@v0.2.0` → `@v0.3.0`.
3. `.venv/bin/pip install -e ".[dev]"`
4. `.venv/bin/python -m pytest` — full suite green (pin bump is non-breaking; no new code yet).
5. Commit: `Bump pare-worker-kit pin to v0.3.0 (open_artifact release)`

---

## Task 2 — Contract entry for `console_capture`

**Files:** `src/pare_hardware_mcp/contract.py`, `tests/unit/test_contract.py`,
`tests/integration/test_wire_contract.py`.

**Context:** the wire `inputSchema` is **derived by FastMCP from the handler's signature**
(`server.py:22-28` passes `name`/`description`/`meta` to `add_tool`, no schema override) — so the
load-bearing reserved-name declaration is the handler signature (Task 3), and the wire-level test
reads `list_tools()` output. The `ToolSpec.input_schema` dict is the contract document, pinned by
its own test. Reserved names are referenced **via the kit constants, never literals** (spec §4,
parent §7 anti-rot). `PRODUCES_ARTIFACT` already exists in the kit (v0.2.0 `artifacts.py:21`),
so no vocabulary work is needed here.

Step 1 — failing tests:

1. `tests/unit/test_contract.py`:
   - `EXPECTED` gains `"console_capture"` (the subprocess test at :79 asserts
     `len == len(EXPECTED)`, so it tracks automatically).
   - Replace `test_every_tool_advertises_a_valid_tier_and_produces_result` (:32-35) with:
     ```python
     def test_every_tool_advertises_a_valid_tier_and_a_known_produces():
         for spec in TOOL_SPECS:
             assert spec.risk_tier in VALID_RISK_TIERS, spec.name
             assert spec.produces in VALID_PRODUCES, spec.name

     def test_console_capture_is_the_only_artifact_producer():
         # The first produces=artifact tool in the fleet. Everything else returns
         # model-consumed results; the artifact channel is the descriptor, nothing else.
         for spec in TOOL_SPECS:
             want = PRODUCES_ARTIFACT if spec.name == "console_capture" else PRODUCES_RESULT
             assert spec.produces == want, spec.name
     ```
     (import `PRODUCES_ARTIFACT, VALID_PRODUCES` from `pare_worker_kit`; drop `PRODUCES_RESULT`
     from the import only if it stops being used — it keeps being used by the new test).
   - The tier test (:45-51) needs **no edit**: `console_capture` lands in
     `EXPECTED - _HIGH - {"console_open"}` and is thereby asserted `low` (spec §4: wire tier
     low, prompt guaranteed by the daemon's artifact floor).
   - Add:
     ```python
     def test_console_capture_declares_the_reserved_artifact_args():
         # Parent §10 Risk-1 mitigation, pinned on the contract document: an author who
         # does not know a name is reserved fails the build. Via the kit constants,
         # not literals (anti-rot).
         from pare_worker_kit import ⟨SLUG_ARG_CONST⟩, ⟨DRIVE_ARG_CONST⟩
         spec = next(s for s in TOOL_SPECS if s.name == "console_capture")
         props = spec.input_schema["properties"]
         assert ⟨SLUG_ARG_CONST⟩ in props
         assert ⟨DRIVE_ARG_CONST⟩ in props
     ```
2. `tests/integration/test_wire_contract.py`:
   - :19 `assert meta[PRODUCES_META_KEY] == PRODUCES_RESULT` → `== spec.produces` (as written,
     it goes red the moment the first non-result tool exists — that is the bug this plan fixes).
   - Add the wire-level reserved-names test (the schema the daemon and model actually see):
     ```python
     async def test_console_capture_advertises_the_reserved_artifact_args():
         # FastMCP derives the wire inputSchema from the handler signature, so this
         # asserts the declaration that actually ships, from list_tools output.
         from pare_worker_kit import ⟨SLUG_ARG_CONST⟩, ⟨DRIVE_ARG_CONST⟩
         server = build_server()
         tools = {t.name: t for t in await server.list_tools()}
         props = tools["console_capture"].inputSchema["properties"]
         assert ⟨SLUG_ARG_CONST⟩ in props
         assert ⟨DRIVE_ARG_CONST⟩ in props
     ```

Run: `.venv/bin/python -m pytest tests/unit/test_contract.py tests/integration/test_wire_contract.py`
→ **RED** (`console_capture` not in `TOOL_SPECS`; surface-set mismatch; KeyErrors in the new tests).

Step 2 — implementation, `contract.py`: extend the kit import line (:18) with
`PRODUCES_ARTIFACT, ⟨SLUG_ARG_CONST⟩, ⟨DRIVE_ARG_CONST⟩`, and insert the entry after
`console_read` (:105), keeping the buffer tools grouped:

```python
    ToolSpec("console_capture", "low",
             "Snapshot a cursor window of this session's capture buffer to a file "
             "on the bench's artifact drive. The result is the artifact DESCRIPTOR "
             "(path, size, sha256, ...) -- the file, not its bytes. SNAPSHOT, NOT "
             "DRAIN: one read of [from_cursor, to_cursor); console_read's cursor is "
             "untouched, and bytes that arrive after the snapshot are what the NEXT "
             "read or capture sees. The window must lie entirely within the "
             "retained buffer: a from_cursor evicted by wraparound is refused "
             "(the file would silently omit its claimed prefix, and the "
             "descriptor has no field to say so), a cursor ahead of the buffer "
             "head is refused rather than clamped, and an empty window is "
             "refused before anything is written. The write is owned by the "
             "artifact mechanism, not this handler; its failures (drive "
             "unmounted, read-only, full, name collision, stale +partial) come "
             "back as named errors. The descriptor does NOT record capture "
             "gaps: query console_read's capture_gaps or console_status for "
             "the same window before relying on the file as unbroken.",
             _in(session={"type": "string"},
                 from_cursor={"type": "integer"},
                 to_cursor={"type": "integer"},
                 name={"type": "string"},
                 **{⟨SLUG_ARG_CONST⟩: {"type": "string"}},
                 **{⟨DRIVE_ARG_CONST⟩: {"type": "string"}}),
             produces=PRODUCES_ARTIFACT),
```

(No `required` list: the reserved args are declared optional in the schema and enforced by the
handler's named refusal — a bare schema rejection would carry no diagnostic. Spec §4 fixes the
declaration, not the optionality.)

Step 3 — `.venv/bin/python -m pytest` full suite **GREEN**.
Step 4 — Commit: `Add console_capture to the contract (first produces=artifact tool)`

---

## Task 3 — Handler: happy path, snapshot semantics, relationship test

**Files:** `pyproject.toml` (dev extra), `tests/unit/test_tools_capture.py` (new),
`src/pare_hardware_mcp/tools.py`.

**Context:** unit-level tests only — a `FakeSession` carrying a **real** `CaptureBuffer`
(pattern of `tests/unit/test_tools_read.py:14-44`) and a **tmpdir root with a `.bench-store-id`
sentinel**: `open_artifact` requires the sentinel, not a mount (parent §5 step 2), so no loopback
rig is needed here. `Config` is frozen (`config.py:21`), so tests replace the whole `tools.CONFIG`
object (the pattern documented at `tools.py:733-742`). The relationship test needs agent_core —
test-only dependency, so it never reaches the Pi (the deploy script's `pip install -e` takes no
extras).

Step 1 — dev dependency: `pyproject.toml`
`dev = ["pytest", "pytest-asyncio"]` →
`dev = ["pytest", "pytest-asyncio", "agent_core @ git+https://github.com/EdibleTuber/agent_core.git@⟨AGENT_CORE_TAG⟩"]`,
then `.venv/bin/pip install -e ".[dev]"`.

Step 2 — failing tests, `tests/unit/test_tools_capture.py`:

```python
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

from pare_worker_kit import PRODUCES_ARTIFACT, ⟨SLUG_ARG_CONST⟩, ⟨DRIVE_ARG_CONST⟩

SENTINEL = "b2657680-9f16-4918-9195-ef3ba17da924"
SLUG = "wbench-test"  # must satisfy the kit's SLUG_RE -- check at run time


def reserved():
    """The two daemon-injected arguments, at their wire names (ledger)."""
    return {⟨SLUG_ARG_CONST⟩: SLUG, ⟨DRIVE_ARG_CONST⟩: SENTINEL}


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
    from agent_core.⟨tools module⟩ import ⟨WORKER_TOOL_SPEC⟩
    mgr, root = rig
    mgr.current.buffer.append(b"payload")
    out = json.loads(await tools.console_capture(session="s-1", name="rel", **reserved()))
    spec = WorkerSpec(⟨minimal streamable_http spec⟩,
                      artifact_root=str(root), artifact_drive_id=SENTINEL,
                      artifact_host="100.97.133.126")
    tool = ⟨WORKER_TOOL_SPEC⟩(name="console_capture", produces=PRODUCES_ARTIFACT)
    validate_descriptor(out, spec=spec, tool=tool)   # no exception IS the assertion


async def test_a_mismatched_drive_id_fails_validation(rig):
    # The negative half: the relationship must be able to FAIL. If the landed
    # validator does not reject a drive_id mismatch, STOP and surface it --
    # do not quietly swap in a different field.
    from agent_core.workers.artifacts import validate_descriptor
    from agent_core.workers.types import WorkerSpec
    from agent_core.workers.⟨artifacts module⟩ import ⟨DAEMON_ARTIFACT_ERROR⟩
    from agent_core.⟨tools module⟩ import ⟨WORKER_TOOL_SPEC⟩
    mgr, root = rig
    mgr.current.buffer.append(b"payload")
    out = json.loads(await tools.console_capture(session="s-1", name="rel", **reserved()))
    spec = WorkerSpec(⟨minimal streamable_http spec⟩,
                      artifact_root=str(root),
                      artifact_drive_id="0361c41f-e680-4d4e-b9c3-39af8a33d067",
                      artifact_host="100.97.133.126")
    tool = ⟨WORKER_TOOL_SPEC⟩(name="console_capture", produces=PRODUCES_ARTIFACT)
    with pytest.raises(⟨DAEMON_ARTIFACT_ERROR⟩):
        validate_descriptor(out, spec=spec, tool=tool)
```

(⟨minimal streamable_http spec⟩: the fewest fields the landed `WorkerSpec` accepts for a
`transport="streamable_http"` entry — `name`, `endpoint`, `risk_default` at minimum, per
`PARE/tests/test_workers_yaml.py`; trim to what the model requires and note it in the ledger.)

Run: `.venv/bin/python -m pytest tests/unit/test_tools_capture.py -x` → **RED**
(`AttributeError: module 'pare_hardware_mcp.tools' has no attribute 'console_capture'`).

Step 3 — implementation, `tools.py`: add `open_artifact` to the kit import (ledger line), and
insert after the `console_read` handler (before `console_status`), mirroring
`console_read`'s structure (`:152-201`):

```python
async def console_capture(session: str, name: str, from_cursor: int = 0,
                          to_cursor: int | None = None,
                          ⟨slug_param⟩: str | None = None,
                          ⟨drive_param⟩: str | None = None) -> str:
    """Snapshot a cursor window of the session's capture buffer to the
    artifact drive. The result is the descriptor, not the bytes. See the
    contract entry for the wire description."""
    try:
        sess = await asyncio.to_thread(MANAGER.get, session)
    except KeyError:
        return _err(f"no such session {session!r}")
    try:
        head = sess.buffer.head
        to = head if to_cursor is None else to_cursor
        # One read of [from_cursor, to): snapshot, not drain. Inline, like
        # console_read's -- the buffer lock is a leaf held for a memory copy.
        data, _next, _dropped, _remaining = sess.buffer.read(
            from_cursor, to - from_cursor)
    except SessionError as exc:
        # A close landed between MANAGER.get returning and the buffer being
        # read (spec §5.6) -- the same catch console_read carries.
        return _err(str(exc))

    def _write():
        # The write is owned by the artifact mechanism: sentinel check,
        # O_CREAT|O_EXCL temp, inline hash, expected_size short-write check,
        # RENAME_NOREPLACE. Off the loop: it is real disk I/O.
        with open_artifact(CONFIG.artifact_root, ⟨slug_param⟩, name,
                           media_type="application/octet-stream",
                           expect_drive_id=⟨drive_param⟩,
                           expected_size=len(data)) as art:
            art.write(data)
        return art.descriptor

    return _ok(**await asyncio.to_thread(_write))
```

(Pre-checks and the kit-error pass-through are deliberately **not** here yet — Tasks 4 and 5
add them with their own RED tests. At this stage a bad cursor still surfaces as `CursorError`
from the buffer read; Task 4 replaces that with explicit checks. `to - from_cursor` going
negative is exercised only by Task 4's empty-window tests, which are RED against this code.)

Step 4 — `.venv/bin/python -m pytest` full suite **GREEN**.
Step 5 — Commit: `Implement console_capture: snapshot read + kit write + descriptor result`

---

## Task 4 — Pre-open refusals (spec §5.1–5.5)

**Files:** `tests/unit/test_tools_capture.py`, `src/pare_hardware_mcp/tools.py`.

**Context:** every refusal is a **distinct** named error, none clamped (spec §4-5). Order is the
spec's: 1 no session (already in), 2 root undeclared, 3 invalid cursor, 4 evicted, 5 empty —
then the snapshot read. One addition, flagged in *Spec notes* below: a declared root reached
without the daemon's reserved-argument injection is a wiring fault and gets its own refusal,
right after check 2. The post-read `dropped` check is the authority against a wrap landing
between the pre-check and the read (the reader thread keeps appending; the pre-check and the
read are separate lock acquisitions).

Step 1 — failing tests (append to `test_tools_capture.py`):

```python
async def test_undeclared_root_is_refused_before_anything_is_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "CONFIG", Config())   # artifact_root is None
    monkeypatch.setattr(tools, "MANAGER", FakeManager())
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert "PARE_HW_ARTIFACT_ROOT" in out["error"]
    assert not any(tmp_path.iterdir())              # nothing opened, nothing written


async def test_missing_reserved_args_are_refused(rig):
    # A declared root reached without the daemon's injection is a wiring fault:
    # fail closed with its own named error, not a kit crash.
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a"))
    assert "reserved artifact arguments" in out["error"]
    assert not (root / SLUG).exists()


async def test_negative_cursor_is_refused(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", from_cursor=-1, **reserved()))
    assert "invalid cursor" in out["error"]
    assert not (root / SLUG).exists()


async def test_from_ahead_of_head_is_refused_not_clamped(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", from_cursor=99, **reserved()))
    assert "invalid cursor" in out["error"]
    assert not (root / SLUG).exists()


async def test_to_ahead_of_head_is_refused_not_clamped(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", to_cursor=99, **reserved()))
    assert "invalid cursor" in out["error"]
    assert not (root / SLUG).exists()


async def test_evicted_window_is_refused_with_the_oldest_retained_named(rig):
    mgr, root = rig
    mgr.current = FakeSession(capacity=64)
    mgr.current.buffer.append(b"x" * 100)           # head=100, oldest=36
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", from_cursor=0, **reserved()))
    assert "36" in out["error"]                     # oldest retained, named
    # and the re-request from the oldest retained cursor succeeds:
    await tools.console_capture(session="s-1", name="a", from_cursor=36, **reserved())
    assert (root / SLUG / "a").read_bytes() == b"x" * 64


async def test_empty_window_is_refused_before_the_artifact_is_opened(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", from_cursor=2, to_cursor=2, **reserved()))
    assert "empty" in out["error"]
    assert not (root / SLUG).exists()


async def test_window_to_the_head_is_empty_when_from_is_the_head(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")              # head=4
    out = json.loads(await tools.console_capture(
        session="s-1", name="a", from_cursor=4, **reserved()))  # to defaults to 4
    assert "empty" in out["error"]


async def test_a_wrap_landing_between_precheck_and_read_is_refused(tmp_path, monkeypatch):
    # Deterministic reproduction of the reader thread landing between the
    # handler's pre-check and its read: a real CaptureBuffer whose read()
    # first appends enough to wrap from_cursor out (real wrap, real dropped
    # count -- the subclass only schedules the append).
    root = tmp_path / "bench-store"
    root.mkdir()
    (root / ".bench-store-id").write_text(SENTINEL + "\n")
    monkeypatch.setattr(tools, "CONFIG", Config(artifact_root=str(root)))

    class _FillingBuffer(CaptureBuffer):
        def __init__(self):
            super().__init__(capacity=64)
            self.append(b"x" * 60)                  # head=60: from=0 passes pre-checks
        def read(self, cursor, limit=None):
            self.append(b"y" * 10)                  # head=70, oldest=6: wrap lands
            return super().read(cursor, limit)

    mgr = FakeManager()
    mgr.current = FakeSession()
    mgr.current.buffer = _FillingBuffer()
    monkeypatch.setattr(tools, "MANAGER", mgr)
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert "oldest retained" in out["error"]        # the eviction refusal, again
    assert not (root / SLUG).exists()
```

Run: `.venv/bin/python -m pytest tests/unit/test_tools_capture.py -x` → **RED** (root test:
kit crash/`open_artifact(None, …)`; evicted test: a silently shorter file is written; empty
test: negative limit; race test: a shorter file is written).

Step 2 — implementation: in `console_capture`, after the `KeyError` block insert the checks,
and restructure the read block:

```python
    # Pre-checks (spec §5.1-5.5), cheapest first, before anything is opened.
    # The descriptor channel has no field to say "the file starts later than
    # asked", so a window that cannot be written whole is refused, never
    # silently shortened.
    if CONFIG.artifact_root is None:
        return _err("no artifact root declared on this worker: set "
                    "PARE_HW_ARTIFACT_ROOT in the unit environment (and "
                    "artifact_root in workers.yaml)")
    if not ⟨slug_param⟩ or not ⟨drive_param⟩:
        return _err("the daemon did not inject the reserved artifact "
                    "arguments (slug, expected drive id); this worker was "
                    "reached without artifact dispatch wiring")
    try:
        head = sess.buffer.head
        to = head if to_cursor is None else to_cursor
        oldest = max(0, head - sess.buffer.capacity)
        if from_cursor < 0 or from_cursor > head or to < 0 or to > head:
            # Neither is a position this API ever handed out: refused, not
            # clamped (spec §4) -- same surface as console_read's.
            return _err(f"invalid cursor for session {session!r} "
                        f"(from_cursor={from_cursor}, to_cursor={to}, head={head})")
        if from_cursor < oldest:
            return _err(f"from_cursor {from_cursor} is before the oldest "
                        f"retained byte {oldest}: {oldest - from_cursor} bytes "
                        f"were evicted by wraparound. Re-request from "
                        f"from_cursor={oldest}; the evicted prefix is gone.")
        if to <= from_cursor:
            return _err(f"empty window (from_cursor {from_cursor} >= "
                        f"to_cursor {to}): a zero-byte artifact would "
                        f"attest nothing")
        data, _next, dropped, _remaining = sess.buffer.read(
            from_cursor, to - from_cursor)
        if dropped:
            # A wrap landed between the pre-check and this read: the read is
            # clamped and would write a shorter file than claimed. Refuse
            # with the eviction refusal; `oldest` advanced under us.
            return _err(f"from_cursor {from_cursor} is before the oldest "
                        f"retained byte {from_cursor + dropped}: {dropped} "
                        f"bytes were evicted by wraparound. Re-request from "
                        f"from_cursor={from_cursor + dropped}; the evicted "
                        f"prefix is gone.")
    except SessionError as exc:
        return _err(str(exc))
```

(The explicit cursor checks make the `CursorError` from the read unreachable for the cases it
used to cover; drop its `except` clause if it becomes dead — verify with the test run.)

Step 3 — full suite **GREEN**. Step 4 — Commit:
`console_capture: refuse undeclared root, missing injection, bad/evicted/empty windows`

---

## Task 5 — Write-phase failures, kit messages verbatim (spec §5.7)

**Files:** `tests/unit/test_tools_capture.py`, `src/pare_hardware_mcp/tools.py`.

**Context:** after the snapshot read, every `open_artifact` failure propagates the **kit's named
message verbatim** — the operator is at the bench and the message is the diagnostic. What the
unit environment can construct **for real**: absent sentinel, wrong sentinel, a `chmod 555` root
(real `EROFS`), a pre-existing final file (real `EEXIST`), a pre-existing `+partial`. `ENOSPC`
is the kit-owned case (loopback rig, parent P2): the handler test injects the kit's **own**
exception at the one seam the environment cannot produce — that is not mocking the handler, it
is handing the handler a real kit failure. (No test asserts orphan cleanup: the `+partial` left
in place is the parent's deliberate policy, pinned by the kit's own P2 tests; this handler
contains no cleanup code by design.)

Step 1 — failing tests (append):

```python
import os

async def test_absent_sentinel_is_named(rig):
    mgr, root = rig
    (root / ".bench-store-id").unlink()
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert ".bench-store-id" in out["error"]
    assert not (root / SLUG).exists()


async def test_mismatched_sentinel_names_both_uuids(rig):
    mgr, root = rig
    (root / ".bench-store-id").write_text("0361c41f-e680-4d4e-b9c3-39af8a33d067\n")
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert SENTINEL in out["error"]
    assert "0361c41f" in out["error"]


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root bypasses mode bits; EROFS is untestable")
async def test_readonly_root_is_named_as_readonly(rig):
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    os.chmod(root, 0o555)
    try:
        out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    finally:
        os.chmod(root, 0o755)          # a 555 dir is not unlinkable by its owner
    # Pin the kit's actual wording at run time (the test discriminates today
    # only that it is a NAMED readonly error, not a transport failure).
    assert "read-only" in out["error"] or "EROFS" in out["error"]
    assert not (root / SLUG).exists()


async def test_name_collision_is_refused_and_the_file_untouched(rig):
    mgr, root = rig
    dest = root / SLUG / "a"
    dest.parent.mkdir()
    dest.write_bytes(b"someone else's artifact")
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert "a" in out["error"]          # the collision names the artifact
    assert dest.read_bytes() == b"someone else's artifact"   # RENAME_NOREPLACE: no overwrite


async def test_a_stale_partial_is_named_and_left_in_place(rig):
    mgr, root = rig
    partial = root / SLUG / "a+partial"
    partial.parent.mkdir()
    partial.write_bytes(b"orphan")
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert "a+partial" in out["error"]  # named, not assumed abandoned
    assert partial.read_bytes() == b"orphan"


async def test_enospc_reaches_the_model_verbatim(rig, monkeypatch):
    # ENOSPC is the kit-owned case (loopback, parent P2); the unit environment
    # cannot fill a filesystem, so the seam is injected with the kit's OWN
    # exception carrying its OWN message (⟨ENOSPC_MSG⟩ from the ledger). The
    # handler's job is to surface it verbatim (spec §5.7), not to translate
    # or wrap it.
    from pare_worker_kit import ⟨ARTIFACT_ERROR⟩
    def _full(*args, **kwargs):
        raise ⟨ARTIFACT_ERROR⟩(⟨ENOSPC_MSG⟩)
    monkeypatch.setattr(tools, "open_artifact", _full)
    mgr, root = rig
    mgr.current.buffer.append(b"abcd")
    out = json.loads(await tools.console_capture(session="s-1", name="a", **reserved()))
    assert out["error"] == ⟨ENOSPC_MSG⟩   # verbatim, byte for byte
```

Run: `.venv/bin/python -m pytest tests/unit/test_tools_capture.py -x` → **RED** (the
`⟨ARTIFACT_ERROR⟩` escapes the handler as an unhandled exception; the result is never `{"error": …}`).

Step 2 — implementation: wrap the write phase:

```python
    def _write():
        with open_artifact(CONFIG.artifact_root, ⟨slug_param⟩, name,
                           media_type="application/octet-stream",
                           expect_drive_id=⟨drive_param⟩,
                           expected_size=len(data)) as art:
            art.write(data)
        return art.descriptor

    try:
        return _ok(**await asyncio.to_thread(_write))
    except ⟨ARTIFACT_ERROR⟩ as exc:
        # The kit's named messages ARE the diagnostics (spec §5.7): surface
        # verbatim. No wrapping, no rewording, no second validation layer.
        return _err(str(exc))
```

If the gate check recorded more than one exception class, catch exactly those — and if any
failure case surfaces a raw `OSError`, that contradicts the parent spec's named-message promise:
**stop and surface**, don't catch-and-massage.

Step 3 — full suite **GREEN**. Step 4 — Commit:
`console_capture: propagate the kit's named artifact failures verbatim`

---

## Task 6 — Unit env + hardening hole + config docstring

**Files:** `systemd/pare-hardware-mcp.service`, `src/pare_hardware_mcp/config.py`.

**Context:** the spec (§6) names the `Environment=` line; the unit's existing
`ProtectSystem=strict` block (:68) makes the whole tree read-only, so the Environment line alone
would leave the sentinel readable but every write an `EROFS` — `bench_status` would report
`writable: false` and every capture would fail. `ReadWritePaths=` is the unit's own idiom for
this (the `StateDirectory=` comment at :75-79 explains the pattern). See *Spec notes*.

Steps:

1. `systemd/pare-hardware-mcp.service`: after `Environment=PARE_HW_EXPECT_SERIAL=TG1119e7` (:43):
   ```
   # Artifact root: console_capture writes under it via pare-worker-kit's
   # open_artifact (one file per capture, at {root}/{slug}/{name});
   # bench_status inspects it read-only. The drive is fstab-mounted nofail
   # (PARE bench/INSTALL.md:186); a Pi that boots without it has an empty
   # directory there, and the sentinel-absent error is the named outcome.
   Environment=PARE_HW_ARTIFACT_ROOT=/mnt/bench-store
   ```
   and in the hardening block, after `ReadOnlyPaths=/opt/pare/hardware-mcp` (:70):
   ```
   # ProtectSystem=strict makes the whole tree read-only; without this hole
   # the sentinel is readable but every capture write is EROFS, and
   # bench_status reports writable: false on an otherwise healthy bench.
   ReadWritePaths=/mnt/bench-store
   ```
2. `config.py:64-69` docstring — replace the "artifacts are out of scope (see contract.py)"
   claim with the consumer named:
   ```python
       # PARE_HW_ARTIFACT_ROOT: the artifact drive root. console_capture
       # writes under it via pare-worker-kit's open_artifact (one file per
       # capture, at {root}/{slug}/{name}); bench_status inspects it
       # read-only (present, writable, .bench-store-id). `None` when unset
       # is a normal state out of process and a NAMED refusal at capture
       # time -- the daemon refuses an undeclared workers.yaml root before
       # dispatch (parent spec §6 step 2), and bench_status's
       # declared/present/writable/drive_id report is how the operator
       # reconciles the two declarations.
   ```
3. Verification (no new unit test; the deploy-manifest guard already covers the unit's place in
   `FILES`): baseline-diff `systemd-analyze verify systemd/pare-hardware-mcp.service`
   before/after the edit (agenthost is a systemd box; expect the same pre-existing warnings,
   nothing new), then `.venv/bin/python -m pytest` full suite green.
4. Commit: `Unit: declare PARE_HW_ARTIFACT_ROOT and its ReadWritePaths hole`

---

## Task 7 — PARE `workers.yaml`: the hardware entry's artifact fields

**Files:** `/mnt/secondary/projects/PARE/workers.yaml` (:125),
`/mnt/secondary/projects/PARE/tests/test_workers_yaml.py`.

**Context:** inert until dispatch wiring lands (spec §3: either deploy order is safe, both
fail-closed), so this task can execute before or after upstream A — but **bench acceptance
(Task 8) needs it**. The `artifact_root` / `artifact_drive_id` / `artifact_host` fields already
exist on `WorkerSpec` (pre-wiring), so the format-comment test
(`tests/test_workers_yaml_format_comment.py`) stays green untouched. The sentinel value is the
one **re-read from the drive at deploy time** (Task 8 step 1); the yaml carries the recorded
value from `bench/INSTALL.md:159`, with the re-read rule in a comment.

Step 1 — failing test, append to `tests/test_workers_yaml.py`:

```python
def test_hardware_declares_its_artifact_root_and_drive():
    spec = _reg().get("hardware")
    assert spec.artifact_root == "/mnt/bench-store"
    # The sentinel content on the drive (bench/INSTALL.md:157-159, recorded
    # after the 2026-09-27 swap). It is RE-READ FROM THE DRIVE at deploy
    # time, never typed from memory: a drive swap must change the yaml and
    # this pin together, and this test is what keeps them honest.
    assert spec.artifact_drive_id == "b2657680-9f16-4918-9195-ef3ba17da924"
    # Deliberately NOT declared: the parent's A2 default (the endpoint's
    # hostname) is the choice. An explicit artifact_host here would drift.
    assert spec.artifact_host is None
```

Run: `cd /mnt/secondary/projects/PARE && .venv/bin/python -m pytest tests/test_workers_yaml.py -x`
→ **RED** (`spec.artifact_root` is `None`).

Step 2 — edit `workers.yaml` :125, replacing
`artifact_root: null    # placeholder; wiring is a separate design`:

```yaml
    artifact_root: /mnt/bench-store
    # Sentinel content of the bench drive (bench/INSTALL.md:157-159, the
    # 2026-09-27 1 TB replacement). RE-READ FROM THE DRIVE at deploy time --
    # never typed from memory: `cat /mnt/bench-store/.bench-store-id`.
    artifact_drive_id: b2657680-9f16-4918-9195-ef3ba17da924
```

Step 3 — `cd /mnt/secondary/projects/PARE && .venv/bin/python -m pytest` full suite **GREEN**
(WorkerRegistry loads the file on the real pin; the format-comment and risk-override tests are
the canaries).
Step 4 — Commit (in the PARE repo): `workers.yaml: declare the hardware worker's artifact root and drive`

---

## Task 8 — Bench acceptance (operator)

**Executed by the operator at the bench; the agent records and verifies results.** Requires
Tasks 1-7 merged **and** the parent's P1-P4 live (daemon dispatching artifacts). The Pi is
reached over Tailscale; SSH from agenthost may need the operator's browser re-auth (the
known ACL `check`).

1. **Re-read the sentinel (the rule):** on the Pi, `cat /mnt/bench-store/.bench-store-id`.
   It must equal `workers.yaml`'s `artifact_drive_id`. If not: the drive changed since
   `bench/INSTALL.md` was written — update the yaml (and its test), re-deploy PARE, re-run this.
2. Both checkouts clean and at the merged commits (`bench_deploy.sh` refuses a dirty tree).
3. On the Pi, from the hardware-repo checkout: `./scripts/bench_deploy.sh --check`, then
   `sudo ./scripts/bench_deploy.sh`. The script installs the unit (→ `daemon-reload`), re-pips
   the venv (fetches the kit v0.3.0 tag — the Pi needs tailnet network), and stamps.
4. `sudo systemctl restart pare-hardware-mcp` (the script deliberately does not start the unit).
5. Restart the PARE daemon so it loads the new `workers.yaml` fields.
6. Pty pair on the Pi: `socat -d -d pty,raw,echo=0 pty,raw,echo=0` — note a `/dev/pts/N` path.
7. Drive the agent (agenthost, PARE TUI): load the hardware worker, `console_open`
   (device=`/dev/pts/N`, baud 115200), write a known multi-KB string to the pty master, then
   `console_capture` with a meaningful `name`. **The operator sees the approval prompt** — wire
   tier `low`, effective tier raised by the daemon's artifact floor (parent §6 step 2); the
   prompt appearing is itself an acceptance point. Approve.
8. The daemon validates the descriptor (no malformed-descriptor refusal) and publishes the
   finding to the workbench.
9. **Read the finding on the Pi's kiosk screen** — the parent §8 two-page rule: status page
   green, handoff offered, workbench reached, finding read. Not an API 200, not a successful
   `PUT`.
10. On the Pi: `ls -la /mnt/bench-store/<slug>/` and `sha256sum` the file; compare with the
    descriptor's `path`/`sha256`/`size`.
11. **Measure the write** (spec §7's `read_timeout` rule): feed the pty until the 64 MiB buffer
    is full (`dd if=/dev/urandom bs=1M count=64 > /dev/pts/<master>`), capture the whole buffer,
    and record the write's wall time against the declared `read_timeout: 60`. Sub-second to a
    few seconds is the expected band; a number near 60 is a finding for the flash spec's
    revisit, not a failure.
12. `console_close`, kill socat, `bench_status`: `declared`/`present`/`writable`/`drive_id`
    all green.
13. Record results in the ledger; close the plan. Note for the record: when the operator's
    targets land on the bench, re-run this acceptance with a **real boot log** — that is the
    meaningful one (spec §8).

---

## Spec notes — two additions the spec's wording does not literally state

Flagged here (and in the handoff) because the spec is approved and the plan should not quietly
amend it. Both are mechanical consequences of existing surfaces and both follow the spec's own
fail-closed, distinct-error discipline:

1. **`ReadWritePaths=/mnt/bench-store`** (Task 6). Spec §6 names only the unit's
   `Environment=` line. The unit's `ProtectSystem=strict` (:68) makes the whole tree read-only,
   so the Environment line alone would make every capture an `EROFS`. The hole is the unit's
   own idiom (`StateDirectory=` at :75-79).
2. **Reserved-args-missing refusal** (Task 4, after pre-check 2). Spec §5's list is the
   operator-facing failure set; a call that reaches the handler with a declared root but without
   the daemon's injection is a daemon-wiring fault, and `open_artifact(slug=None, …)` would
   crash rather than name it. The distinct refusal closes that gap.

(Third, mechanical: the existing "every tool produces result" assertions in
`tests/unit/test_contract.py:32-35` and `tests/integration/test_wire_contract.py:19` are
rewritten in Task 2 — they assert a fleet property that is no longer true by design.)

## Handoff

- **This plan is not executed now.** The gate is unmet as of 2026-10-02 (verified: no kit
  `open_artifact`, agent_core `_REQUIRED` still four fields, PARE dispatch absent). The next
  build is the parent spec's **P1** plan.
- When the gate lifts, execute in a **fresh session** with the executing-plans ledger (the
  project's established pattern), starting at Task 1's gate re-verification. Tasks 1-6 and 8
  live in the hardware repo/bench; Task 7 commits in PARE.
