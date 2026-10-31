#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi
mkdir -p logs
if [[ $# -eq 0 ]]; then
  set -- run
elif [[ "$1" == --* ]]; then
  set -- run "$@"
fi
echo "$(date -Iseconds 2>/dev/null || date) START args=$*" >> logs/scheduler.log
"$PY" -m avidbackup "$@"
code=$?
echo "$(date -Iseconds 2>/dev/null || date) END exit=$code" >> logs/scheduler.log
exit $code
