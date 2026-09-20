#!/usr/bin/env bash
# Deploy pare-hardware-mcp from a git checkout ON THE PI, with provenance.
#
#   ./scripts/bench_deploy.sh --check    # read-only, no sudo, exits 1 on drift
#   sudo ./scripts/bench_deploy.sh       # install what differs, then re-check
#
# Same discipline as PARE's scripts/bench_deploy.sh: deploy from a checkout
# whose HEAD can be read and stamp it, rather than hand-copying files nobody
# can trace back to a commit.
#
# Unlike PARE's script, this one also owns a venv: systemd's ExecStart points
# at $DEST/.venv/bin/pare-hardware-mcp, so after copying files this script
# creates the venv if missing and `pip install -e` the deployed checkout into
# it. It does NOT enable or start the systemd unit -- that is the operator's
# manual step after first deploy (spec §4, Step B), not this script's job.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/pare/hardware-mcp
VENV="$DEST/.venv"
UNIT_DIR=/etc/systemd/system
STAMP="$DEST/DEPLOYED_FROM"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# file-in-repo -> destination
FILES=(
  "pyproject.toml:$DEST/pyproject.toml"
  "README.md:$DEST/README.md"
  "src/pare_hardware_mcp/__init__.py:$DEST/src/pare_hardware_mcp/__init__.py"
  "src/pare_hardware_mcp/baud.py:$DEST/src/pare_hardware_mcp/baud.py"
  "src/pare_hardware_mcp/config.py:$DEST/src/pare_hardware_mcp/config.py"
  "src/pare_hardware_mcp/contract.py:$DEST/src/pare_hardware_mcp/contract.py"
  "src/pare_hardware_mcp/devices.py:$DEST/src/pare_hardware_mcp/devices.py"
  "src/pare_hardware_mcp/ringbuffer.py:$DEST/src/pare_hardware_mcp/ringbuffer.py"
  "src/pare_hardware_mcp/server.py:$DEST/src/pare_hardware_mcp/server.py"
  "src/pare_hardware_mcp/session.py:$DEST/src/pare_hardware_mcp/session.py"
  "src/pare_hardware_mcp/tools.py:$DEST/src/pare_hardware_mcp/tools.py"
  # Task 6 deliverable. This file does not exist in the repo yet -- until it
  # lands, the drift loop below reports it as "MISSING IN REPO", same as
  # PARE's script does for any mapped file that isn't there.
  "systemd/pare-hardware-mcp.service:$UNIT_DIR/pare-hardware-mcp.service"
)

if [ ! -d "$REPO/.git" ]; then
  echo "ERROR: $REPO is not a git checkout. This script deploys FROM a checkout"
  echo "on purpose -- a copied file cannot tell you which commit it came from."
  exit 2
fi

HEAD_SHA="$(git -C "$REPO" rev-parse --short HEAD)"
DIRTY="$(git -C "$REPO" status --porcelain | wc -l)"
echo "checkout $REPO @ $HEAD_SHA${DIRTY:+ ($DIRTY modified)}"
[ -r "$STAMP" ] && echo "deployed: $(head -1 "$STAMP")" || echo "deployed: no provenance stamp yet"
echo

drift=0
declare -a NEED_FILE NEED_UNIT
for pair in "${FILES[@]}"; do
  src="$REPO/${pair%%:*}"; dst="${pair##*:}"
  a=$(sha256sum "$src" 2>/dev/null | cut -c1-12)
  b=$(sha256sum "$dst" 2>/dev/null | cut -c1-12)
  if [ -z "$a" ]; then
    printf '  %-56s MISSING IN REPO\n' "${pair%%:*}"; drift=1; continue
  fi
  if [ "$a" = "$b" ]; then
    printf '  %-56s current\n' "$dst"
  else
    printf '  %-56s STALE (repo %s, deployed %s)\n' "$dst" "$a" "${b:-absent}"
    drift=1
    case "$dst" in
      "$UNIT_DIR"/*) NEED_UNIT+=("$src:$dst") ;;
      *)             NEED_FILE+=("$src:$dst") ;;
    esac
  fi
done

echo
if [ "$drift" -eq 0 ]; then
  echo "Everything deployed matches $HEAD_SHA."
  exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "Drift above. Re-run with sudo (no --check) to install."
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Installing needs root. Re-run: sudo $0"
  exit 1
fi

# Deploying from a dirty tree defeats the point of the provenance stamp: the
# stamped HEAD_SHA would not describe what actually got copied. Override with
# ALLOW_DIRTY=1 for a deliberate exception (e.g. iterating on the Pi itself).
if [ "$DIRTY" -gt 0 ] && [ "${ALLOW_DIRTY:-0}" != "1" ]; then
  echo "ERROR: checkout has $DIRTY modified file(s) relative to $HEAD_SHA."
  echo "Commit or stash first, or set ALLOW_DIRTY=1 to deploy anyway."
  exit 2
fi

for pair in "${NEED_FILE[@]:-}"; do
  [ -z "$pair" ] && continue
  src="${pair%%:*}"; dst="${pair##*:}"
  install -D -m 644 "$src" "$dst"
  echo "  installed $dst"
done

reload=0
for pair in "${NEED_UNIT[@]:-}"; do
  [ -z "$pair" ] && continue
  src="${pair%%:*}"; dst="${pair##*:}"
  install -D -m 644 "$src" "$dst"
  echo "  installed $dst"
  reload=1
done

# Ensure the venv systemd's ExecStart points at exists, then install the
# just-deployed checkout into it as an editable package.
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  echo "  created $VENV"
fi
if ! "$VENV/bin/pip" install -e "$DEST"; then
  echo "ERROR: pip install -e $DEST failed. DEPLOYED_FROM not written."
  exit 1
fi
echo "  installed editable package into $VENV"

# Stamp AFTER the venv install succeeds, so a truthful record only appears
# once the deployed tree is actually importable.
printf '%s  deployed %s from %s\n' "$HEAD_SHA" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$REPO" > "$STAMP"
chmod 644 "$STAMP"
echo "  stamped $STAMP"

if [ "$reload" -eq 1 ]; then
  systemctl daemon-reload
  echo "  systemctl daemon-reload"
  echo "  NOTE: unit file changed. Enabling/(re)starting it is a manual"
  echo "  operator step, not done by this script."
fi

echo
echo "Re-checking:"
exec "$0" --check
