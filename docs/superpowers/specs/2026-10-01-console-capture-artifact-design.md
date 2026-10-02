# Console capture artifact — pare-hardware-mcp artifact wiring, v1

**Date:** 2026-10-01
**Status:** v1 — designed, not implemented. Implementation is hard-gated on the parent spec's P1–P4 (§3).
**Parent:** PARE `docs/superpowers/specs/2026-09-12-artifact-wiring-design.md` (the artifact wiring spec — "v1, designed, not implemented" as of the date below).
**Scope:** `pare-hardware-mcp` (one new tool, contract entry, config, unit, kit pin) plus the `workers.yaml` entry it activates in the PARE repo. No changes to `agent_core` or `pare-worker-kit` — that is the parent spec's work.

## 1. What this spec is, and what it is not

This makes `pare-hardware-mcp` the **first production artifact producer** in the fleet, with exactly one
new tool in v1: `console_capture`. It snapshots a cursor window of an open session's capture buffer
to a file under `{artifact_root}/{slug}/` and returns the parent §4 descriptor as its sole result.

It is not:

- **The flash family.** `flash_*`/`erase_*`/`write_*`/`glitch_*` need the probe spike (B) — a Tigard
  on the bench, and the answer to "what actually reaches this target's flash" (SWD/JTAG header, SPI,
  which backend fits the kit's encapsulated-tool rule). That is a separate spec that **reuses this
  spec's mechanism** and carries the `critical`-tier encapsulated-write design. The reserved risk
  pins in PARE's `workers.yaml` already hold its place.
- **Sweep-evidence artifacts.** The baud-cycling sweep's per-rate samples and ranking stay a *result*
  — they are small and model-consumed, and the tool already returns them.
- **`hash_artifact`** (deferred by the parent) or **`bench_doctor.sh`'s `.partial` check** (PARE repo,
  parent P4's work).

## 2. Why `console_capture`, and why it is worth doing before any flash hardware lands

The capture buffer holds 64 MiB (`ringbuffer.py:39`, `DEFAULT_CAPACITY`); `console_read` is capped at
256 KiB **per call** (`ringbuffer.py:65`, `MAX_READ_LIMIT`, applied at `tools.py:118`). Draining a full
boot log through the model's context is therefore ~256 calls of base64-inflated bytes. The parent spec's
motivating case — *"a two-gigabyte firmware dump would cross the network as one tool result"* (kit
`artifacts.py` docstring) — is the same problem at larger scale, and `console_capture` is its first
real instance at the scale the current hardware already produces.

It works on today's rig: no Tigard, no probe, no flash path. A pty stands in for a target, as the
repo's integration tests already do (`tools.py:15`: *"measured end-to-end over a pty"*).

And it earns its place in the eventual firmware loop: when a target is reflashed, the serial console
is where the flasher's output lands, and that capture is the evidence. The boot log is, in
`ringbuffer.py`'s own words, *"the single most valuable artifact a hardware investigation gets"* —
this tool is what makes it durable.

## 3. Dependencies, version drift, and order

**Hard gate.** Nothing in this spec can be implemented — or usefully tested end-to-end — until the
parent's phases land: its P1 (descriptor field-set and reserved-argument constants on both sides,
`_REQUIRED`, `validate_descriptor`'s new signature), P2 (`open_artifact` in the kit), P3 (dispatch
wiring, reserved-argument injection, the tier floor, slug validation, extraction, refusals), and the
PARE-side slug supply. Verified 2026-10-01 that none of it has landed: agent_core's `_REQUIRED` is
still the original four fields (`artifacts.py:39`), `validate_descriptor` still takes
`worker: str` (`:46`), the kit has no `open_artifact` (only `artifact_path`, `:67`), and
`pool.produces()` (`risk_pool.py:206`) still has zero callers.

**Two version literals in the parent spec are stale, and this spec corrects them here rather than
propagating them:**

- The parent planned to tag the kit `v0.2.0` for the artifact work. **That tag was consumed by
  worker request logging** (`973d7de`, PR #9). `open_artifact` therefore ships in **kit v0.3.0**.
  This worker already pins `v0.2.0` (`pyproject.toml:13`) and bumps to `v0.3.0` in its
  implementation round.
- The parent planned the agent_core artifact release as `v1.11.0`. `1.11.0` (capture off the event
  loop) and `1.11.1` (`/help` table) have shipped on other work (agent_core `git log`: `a5a6129`).
  The artifact release lands at **v1.12.0** — or whatever the parent's P1 phase names it; this spec
  references it as "the agent_core release that lands dispatch wiring" and does not depend on the
  number.

The parent's §7 ordering logic (declarations → guard → behaviour → tags → consumer pin) is unaffected;
only the tag numbers moved.

**One requirement this spec places on the parent's P3 phase, because §5.7 depends on it.** A
`produces=artifact` tool that returns an error object — `{"error": …}`, the surface this worker
already uses for every named failure — must have that error reach the model **verbatim**. It must
not be treated as a malformed descriptor and refused under the parent's generic refusal. §5's
design is a design of *diagnostics*: sentinel mismatch, read-only, `ENOSPC`, collision — each named,
each the operator's next action. If P3's extraction parses every artifact result as the seven-field
descriptor and refuses whatever does not parse, every one of those answers is swallowed behind a
"malformed descriptor" refusal and the worker's named errors never reach anyone. The distinction
is mechanical (an error object has no `path` or `sha256`); the requirement is that P3 make it, and
assert it with a test — a `produces=artifact` tool returning `{"error": "…"}` surfaces `…` intact.
How P3 draws the line is a P3-plan decision; that it is drawn at all is this spec's requirement.

**Order within this spec's own implementation** (all after the gate):

1. Kit pin `v0.2.0` → `v0.3.0` in `pyproject.toml` (after the tag exists), suite green.
2. `console_capture`: contract entry + handler + tests.
3. Unit env `PARE_HW_ARTIFACT_ROOT`, `config.py` docstring, deploy.
4. PARE `workers.yaml`: the hardware entry's `artifact_root` / `artifact_drive_id`.
5. Bench acceptance (§8).

Steps 3–4 can land in **either order and both are safe**: the `workers.yaml` fields are inert until a
tool declares `produces=artifact` (no artifact dispatch → nothing reads them), and a worker shipping
`console_capture` against `artifact_root: null` is refused *before the gate* (parent §6 step 2:
*"No `artifact_root` → refused (§5.4, fail closed)"*). Fail-closed, not fail-open. Both must land
before step 5.

## 4. The tool: `console_capture`

**Wire name** `hardware_console_capture` (the daemon prefixes with the `workers.yaml` key; the
contract name is bare — `contract.py:1-7`, the `hardware_hardware_…` rule).

**`produces: artifact`** — the first tool on this worker to declare it (`ToolSpec.produces` already
carries the field, defaulting to `PRODUCES_RESULT`, `contract.py:34-41`; `server.py:26-27` already
stamps `PRODUCES_META_KEY` onto every tool, so the wire meta exists and is correct).

**Tier: `low` on the wire, prompt guaranteed by the daemon.** The tool only reads its own session's
buffer — it opens no port, touches no relay, sends no bytes to the target. The drive write is owned
by the artifact mechanism, not by this handler. The parent's §6 step 2 raises the effective tier of
*any* artifact dispatch to at least `high`, so the operator prompt is guaranteed by the daemon and
does not depend on this worker's wire tier — which means **no `risk_overrides` pin is needed** for
`hardware_console_capture`, and adding one would document a control that lives elsewhere. (The
entry's `risk_default: high` remains the floor for a tool that fails to advertise a tier; that is a
separate mechanism and is not this decision.)

**Arguments.**

| Argument | Source | Contract |
|---|---|---|
| `session` | model | string, required. An open session id, as every other `console_*` tool takes one. |
| `from_cursor` | model | integer, optional, default `0`. |
| `to_cursor` | model | integer, optional, default = the buffer `head` sampled once at handler start. |
| `name` | model | string, required. The artifact filename. Validated by the kit's `_NAME_RE` inside `open_artifact`. |
| *(reserved 1)* | **daemon** | the slug — parent A4. |
| *(reserved 2)* | **daemon** | the expected drive id — parent A4. |

The two reserved names are **wire vocabulary the parent's P1 phase defines as named constants in the
kit**, and this spec deliberately does not restate them as literals (the parent's §7 anti-rot rule:
*"a literal here would have rotted inside a day"*). The requirement is: **`console_capture`'s
`inputSchema` declares both reserved names, referenced via the kit's named constants, not string
literals.** That declaration is the parent's §10 Risk-1 mitigation — an author who does not know a
name is reserved fails the build, per the parent §6's input-schema assertion — and it is testable
from `list_tools` output, so a contract-level test in this repo pins it alongside the parent's
conformance assertion.

`name` is model-supplied on purpose: a meaningful name is what the agent's workflow needs to refer
back to the artifact later ("the boot log captured before the reflash"). The daemon injects only the
slug and the drive id; a name collision is a distinct `EEXIST` error (§5), not a silent overwrite —
`RENAME_NOREPLACE` (parent §5 step 7) is the control.

**Semantics, as invariants.**

1. **Snapshot, not drain.** Exactly one read of the window `[from_cursor, to_effective)`, where
   `to_effective` is `to_cursor` with the one rule below. The bytes read are the bytes written. The
   reader thread keeps capturing after the snapshot; bytes that arrive later are what the *next*
   read or capture sees. `console_read`'s cursor is untouched — capture neither consumes nor
   advances anything.
2. **The size is known in advance, and that is what makes the short-read check real.**
   `expected_size` is the length of the snapshot, stated before the first byte is written — the
   parent §5's one defence against a short read, satisfied honestly rather than by fiat. The window
   is bounded by the buffer capacity (`buffer_bytes`, default 64 MiB), so the bound is a configured
   number, not a guess.
3. **`media_type` is `application/octet-stream`, decided by the worker.** UART output is arbitrary
   bytes (`tools.py:5-8`), and the parent §5 is explicit that the producing tool supplies the media
   type — it is a fact about what the tool wrote, not a model argument.
4. **The result is exactly the seven worker-reported descriptor fields** — `path`, `size`, `sha256`,
   `hashed_at`, `media_type`, `drive_id`, `host` (parent §4) — as exactly one text content block
   parsing as a JSON object, and **no extras**. The kit's `open_artifact().descriptor` yields exactly
   these (parent §5 signature), so the handler returns them and nothing more. Tool-specific metadata
   (captured range, gap count) has no defined daemon-side semantics as descriptor fields, and
   inventing extras is a contract change that belongs in the parent spec. The metadata the model
   actually needs is already queryable: `console_read` returns `dropped` and `capture_gaps`,
   `console_status` returns `capture_gap_count`/`capture_gap_seconds`/`capture_suspended`
   (`session.py:1281-1286`) — the tool description says to query them, so a capture is never read
   as an unbroken record it is not.

**Cursors, and the one place this tool refuses where `console_read` accommodates.**

- Negative, or ahead of the sampled `head`: `CursorError`, surfaced as an invalid-cursor error,
  exactly as `console_read` does (`tools.py:169-174`). `to_cursor` ahead of `head` is refused rather
  than clamped — a cursor ahead of `head` is not a position this API ever handed out, and clamping
  would hide a caller bug.
- **`from_cursor` before the oldest retained byte (the buffer wrapped past it): refused, with a
  message that names the oldest retained cursor and the evicted byte count.** This is the deliberate
  divergence from `console_read`, which clamps and reports `dropped` in-band — and it is load-bearing.
  `console_read` can be honest because the bytes travel with their metadata into the model's context.
  The artifact's result channel is *exactly the descriptor*: there is no field in which to say
  "this file starts later than you asked." A descriptor attests a named file of a stated size; a
  file that silently omits its claimed prefix is the "boot log with an untold hole" that
  `ringbuffer.py:9-12` exists to prevent, and the artifact path has no other way to tell the hole.
  The refusal is the honest form: the model re-requests from the oldest retained cursor.
- **Empty window (`from_cursor >= to_effective`): refused before the artifact is opened.** A
  zero-byte file would pass the size check with `expected_size=0` while attesting nothing — a
  contract violation in disguise, and the open is the expensive half, so the check comes first.

## 5. Failure modes, each with a distinct error

The operator is at the bench and the message is the diagnostic, so every refusal names its cause.
Pre-checks run before anything is opened, in this order (cheapest and side-effect-free first):

1. **No such session** — `MANAGER.get`'s `KeyError`, same surface and message shape as
   `console_read` (`tools.py:159-161`).
2. **Artifact root undeclared on the worker** — `CONFIG.artifact_root is None`. Distinct message
   naming the unit environment variable (`PARE_HW_ARTIFACT_ROOT`). The daemon refuses an undeclared
   root in `workers.yaml` before the gate (parent §6 step 2); this check covers out-of-process
   exercise (tests, manual runs) and the two-declarations-disagree case, where
   `bench_status`'s existing `declared`/`present`/`writable`/`drive_id` report
   (`tools.py:698-723`) is how the operator reconciles them.
3. **Invalid cursor** — §4's `CursorError` case.
4. **Evicted window** — §4's wrap refusal.
5. **Empty window** — §4's zero-byte refusal.

Then the snapshot read, then the write:

6. **Session ended mid-handler** — a `console_close` landing between `MANAGER.get` returning and the
   buffer being read raises `SessionError` from the buffer property (`session.py:180-193`, caught
   today at `tools.py:175-183`); the capture handler must catch it the same way. There is no await
   between the two in a single-threaded read, but the handler runs the buffer access off the event
   loop per this file's flat rule (`tools.py:10-20`), and the close runs on a thread.
7. **`open_artifact` failures propagate the kit's named messages verbatim** — sentinel absent
   (*"no `.bench-store-id` at `{root}` — is the drive mounted?"*), sentinel mismatch (naming both
   UUIDs), read-only named as read-only (`EROFS`), `ENOSPC`, `EFBIG`, a pre-existing `{name}+partial`
   (naming the file, not assuming it is abandoned), `EEXIST` for a completed artifact in the way,
   and the short-write refusal stating both numbers (parent §5, each distinct). **The `+partial`
   orphan is left in place** — the parent's deliberate orphan policy; its accumulation check is the
   parent's P4 `bench_doctor.sh` work. (Their reaching the model at all is the parent's P3's job —
   §3's explicit requirement: an error object must surface verbatim, not as a malformed-descriptor
   refusal.)

## 6. Worker wiring

- **`contract.py`** gains the `console_capture` entry — `produces=PRODUCES_ARTIFACT`, the argument
  table of §4, and a description that states the snapshot semantics, the cursor refusals, and the
  "query `console_read`/`console_status` for `capture_gaps`" pointer. **The no-hardware rule holds**
  (`contract.py:9-12`): PARE's CI imports this module on a runner with no Pi and no pyserial to
  validate risk pins against real tool names, so the entry may reference the `ringbuffer` constants
  (as `console_read` already does, `contract.py:95-105`) and nothing that reaches a device.
- **`config.py`** — no new field: `artifact_root` already exists (`:64-69`), read from
  `PARE_HW_ARTIFACT_ROOT` at `:146`. Its docstring currently says artifacts are *"out of scope (see
  contract.py)"* and is updated to name the consumer. The handler reads the module-level `CONFIG`
  singleton, the same channel as the other `console_*` tools — not `bench_status`'s fresh
  `load_config()`; that divergence is a pre-existing wart documented at `tools.py:732-742` and this
  spec does not widen or resolve it.
- **`systemd/pare-hardware-mcp.service`** gains
  `Environment=PARE_HW_ARTIFACT_ROOT=/mnt/bench-store`. The drive is fstab-mounted
  `nofail` (PARE `bench/INSTALL.md:186`): a Pi that boots without it has an ordinary empty
  directory there, and §5.7's sentinel-absent error is the named outcome — `bench_status` reports
  the same state read-only, so the operator sees it without a capture attempt.
- **Deployment** is the repo's own `scripts/bench_deploy.sh` (checkout on the Pi →
  `/opt/pare/hardware-mcp`, venv, unit, stamped) plus the operator's `systemctl restart
  pare-hardware-mcp`. Same provenance discipline as PARE's bench deploy script.

## 7. PARE-side wiring

The hardware entry in `workers.yaml` (currently `:111-125`, with
`artifact_root: null  # placeholder; wiring is a separate design` at `:125`) gains:

- **`artifact_root: /mnt/bench-store`** — the operator's declaration in the file the worker cannot
  touch, the trust anchor of the whole mechanism (parent §5 step 1).
- **`artifact_drive_id`** — **required whenever `artifact_root` is set** (parent A5). The value is
  the sentinel file's content, `b2657680-9f16-4918-9195-ef3ba17da924` per PARE
  `bench/INSTALL.md:159`, recorded after the 2026-09-27 drive replacement (1 TB, ext4,
  `LABEL=bench-store`). It is **re-read from the drive at deploy time, never typed from memory** —
  the parent §8's rule, stated for its own Level-2 line and binding here. Two disambiguations,
  because the bench now carries three UUIDs and a wrong pick fails the sentinel comparison at
  dispatch with a message naming both:
  - The parent spec's Level-2 acceptance line names `0361c41f-e680-4d4e-b9c3-39af8a33d067` —
    the **pre-swap** drive, no longer on the bench.
  - The fstab partition UUID (`62060d01-…`, `bench/INSTALL.md:158`) is the filesystem's identity,
    **not** the sentinel; the daemon compares against the sentinel's content.
- **`artifact_host`: not declared.** The parent's A2 default applies — the endpoint's hostname,
  `100.97.133.126`, the tailnet address the retrieval command runs from on `agenthost`. No tailnet
  ambiguity, no extra declaration to drift. (An explicit `pare-bench` declaration is possible and
  admitted by `_HOST_RE`; it buys nothing here.)
- **`read_timeout` stays `60` in v1, with the rule recorded.** The parent §6's requirement: an
  artifact-producing worker's `read_timeout` must exceed its worst-case artifact write with real
  margin, **stated as a rule tied to the measured rate, not a literal that rots**. v1's worst case
  is one 64 MiB snapshot to the bench SSD — sub-second to a few seconds, verified by measurement at
  acceptance (§8), inside `60` with a large margin. The number that bites is the flash family's —
  a 2 GB dump at the parent's measured ~28 MB/s is ≈70 s, *over* 60 — and that revisit belongs to
  the flash spec, with the flash dump's rate measured on this bench. `RENAME_NOREPLACE`
  (parent §5 step 7) is the backstop that keeps a wrong bound survivable either way.

**No PARE code changes in this spec.** `/health` already prints each worker's *declared* root
(`pare/commands/health.py:130-151`), so declaring the field stops the hardware line being skipped;
its "not checked here" caveat stays true of `/health` by design, because the live answer is the
worker's own `bench_status`, which exists and reports it.

## 8. Acceptance

**Worker-level (this repo), TDD, every regression test verified failing against pre-change code
where it discriminates:**

- A **tmpdir root with a `.bench-store-id` sentinel** is a sufficient rig for the handler:
  `open_artifact` requires the sentinel, not a mount (parent §5 step 2). The loopback filesystem
  rig — `truncate`, `mkfs.ext4`, mount — is the parent's P2 work for the *kit*; what the handler
  tests need is each named failure, and the ones constructible without a mount are real, not mocked:
  a `chmod 555` root is a real `EROFS`, a pre-existing file is a real `EEXIST`, a wrong or absent
  sentinel is a real sentinel error. `ENOSPC` is the kit-owned case (loopback, parent P2); the
  handler test asserts the translation of the kit's named error into §5's error surface.
- **The relationship test, per the parent §8's discipline** (*"assert the relationship — a
  descriptor the kit produces validates against the daemon's validator — because a literal breaks on
  the next legitimate change and a relationship survives it"*): a descriptor produced through
  `open_artifact` against the tmpdir rig validates under agent_core's `validate_descriptor`. No
  assertion on a count of fields.
- Every refusal of §5, each with its distinct message.
- The **no-hardware import rule**: `contract.py` still imports on a runner without pyserial.
- The **reserved-names declaration**: `console_capture`'s `inputSchema` declares both reserved
  argument names via the kit's constants.

**Bench (operator).** A pty-backed session on the Pi — no target is on the bench yet, and a pty pair
standing in for the target is exactly what this repo's integration tests already do —
`console_capture` against the **real drive**, the daemon validating the descriptor, the finding
published to the workbench, and **read on the Pi's kiosk screen**: the parent §8's two-page rule
(status page green, handoff offered, workbench reached, finding read). Not an API 200, not a
successful `PUT`. When the operator's targets land on the bench, the same acceptance run with a real
boot log is the meaningful one.

**No implementation code in this spec.** The rule the parent stated for `open_artifact`
(*"code written into prose is unexecuted … and then looks authoritative and gets transcribed
faithfully"*) is about a security invariant with a race in it — and that invariant is the parent's
P2, not this tool's. `console_capture` is mechanical plus a contract surface; its implementation
plan is an ordinary TDD plan and may include code, per the project's phase convention.

## 9. What this cannot establish

**Everything the parent's §9 says, inherited in full**: containment against a *compromised* worker
is not a boundary (the worker calls `open_artifact` by choice; a compromised one hand-writes a
descriptor under a symlink and every daemon-side check passes); `sha256` is integrity across the
transfer and a stable audit identity, **not authenticity**; and the retrieval window is hours, not
milliseconds — anything with write access inside the root can swap the file between capture and the
operator's `scp`.

**Plus the hardware-specific half, stated once:**

- **The file is what the ring buffer held at the snapshot moment.** A wrap-evicted prefix is gone
  before the capture exists — §4 refuses rather than writes a shorter file than claimed, but the
  eviction itself is not recoverable by any mechanism in this spec. A suspended window (a baud scan
  pausing the reader) is a hole in *time*, not in *bytes*: the file is contiguous,
  `capture_gaps` is the only record of the hole, and it is not in the file. The descriptor attests
  the file's integrity; it does not attest the boot's completeness.
- **That the captured bytes are the target's output.** That is established by the operator-declared
  `PARE_HW_DEVICE` plus the `PARE_HW_EXPECT_SERIAL` assertion at `console_open` — already this
  worker's job, already load-bearing — not by the artifact mechanism.

## 10. Risks

- **The parent spec's release plan moved under this one.** Two tag numbers (§3). The logic is
  intact; the numbers are corrected here, and this spec's implementation steps reference the
  releases by what they contain, not by number, so a further slip costs a re-read, not a redesign.
- **The result-shape rule has no build-time enforcement** until the parent's invocation harness
  exists (parent §10). `console_capture`'s descriptor shape is therefore enforced at dispatch, at
  runtime; the worker-level relationship test is the backstop, and a wrong shape that slips past it
  fails loudly at the bench with the parent's refusal-and-audit behaviour, not silently.
- **Orphan accumulation is inherited.** A failed capture leaves a `{name}+partial` of up to
  `buffer_bytes` (64 MiB) under the project directory, deliberately uncleaned. The bench-level
  answer is the parent's P4 `bench_doctor.sh` check; until it lands, `bench_status`'s free-space-
  adjacent report and a plain `ls` at the bench are the operators' eyes.
- **Single-session model removes a whole race class, and the spec should not pretend otherwise.**
  `SessionManager` holds exactly one session (`session.py:1273-1278`), so there is no
  multi-session capture race to design against. There *is* the one race §5.6 names — a close
  landing mid-handler — and it is handled by the same catch `console_read` already carries.
- **Model-supplied `name` is an untrusted string, and the control is the kit's, not this spec's.**
  `_NAME_RE` bounds it (128 characters, controlled alphabet, `pare_worker_kit/artifacts.py:53`);
  the `open_artifact` walk never resolves any component after `root` through a symlink; and
  correctness does not depend on names being unique — `RENAME_NOREPLACE` refuses collisions rather
  than reasoning about them. Stated so the implementer does not reach for a second validation layer
  that would drift from the first.

## 11. Provenance

**Every file:line in this document was read on 2026-10-01**, not recalled:

- Parent spec, all 903 lines, `PARE/docs/superpowers/specs/2026-09-12-artifact-wiring-design.md`.
- `pare-worker-kit`: `artifacts.py` (`_REQUIRED`-era `artifact_path` at `:67`, `SLUG_RE` at `:33`,
  `_NAME_RE` at `:53`), `__init__.py` (`__version__ = "0.2.0"`), `git tag` (`v0.2.0` = `973d7de`,
  the request-logging merge).
- `agent_core`: `git log` (`a5a6129` = release 1.11.1), `workers/artifacts.py` (`_REQUIRED` still
  four fields at `:39`; `validate_descriptor(payload, *, worker, tool)` at `:46`),
  `workers/risk_pool.py` (`produces()` at `:206`, zero callers).
- `PARE`: `workers.yaml:111-125` (hardware entry, `artifact_root: null`),
  `pare/commands/health.py:130-151` (declared-root reporting), `bench/INSTALL.md:157-159,186,194`
  (the 2026-09-27 drive, its sentinel, the fstab line, the sentinel recipe).
- `pare-hardware-mcp`: `contract.py` (all 136 lines), `config.py` (all 152 lines),
  `ringbuffer.py` (all 182 lines), `tools.py` (`console_read` `:152-201`,
  `_artifact_root_status` `:698-723`, `bench_status` `:726-785`, the module rule `:10-20`),
  `session.py` (`SessionManager` `:1273+`, buffer property `:180-193`), `server.py` (all 33 lines),
  `pyproject.toml:13` (kit pin `v0.2.0`), `systemd/pare-hardware-mcp.service` (current
  `Environment=` set — no `PARE_HW_ARTIFACT_ROOT`), `scripts/bench_deploy.sh` (deploy surface).

**Machine state** — the drive's sentinel as recorded in `bench/INSTALL.md` §5 after the 2026-09-27
replacement — is re-read from the drive at deploy time (§7), per the rule, rather than carried over
from this document.
