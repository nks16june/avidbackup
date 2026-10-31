#!/bin/sh
set -eu
# Refuse non-loopback / published UI without a token (defense in depth for Docker).
if [ -z "${AVIDBACKUP_UI_TOKEN:-}" ]; then
  echo "ERROR: Set AVIDBACKUP_UI_TOKEN before publishing the UI from Docker." >&2
  echo "Example: docker run -e AVIDBACKUP_UI_TOKEN=secret -p 8787:8787 -v avidbackup-data:/data ..." >&2
  exit 2
fi
if [ ! -f "${AVIDBACKUP_HOME:-/data}/accounts.json" ]; then
  avidbackup init || true
fi
exec avidbackup "$@"
