#!/usr/bin/env bash
# AvidBackup installer — macOS & Linux
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> AvidBackup install from $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ first."
  echo "  macOS:  brew install python"
  echo "  Debian: sudo apt install python3 python3-venv python3-pip"
  echo "  Fedora: sudo dnf install python3 python3-pip"
  exit 1
fi

PY=python3
ver="$($PY -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
echo "    Python $ver"

$PY -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"

mkdir -p config data logs
if [[ ! -f accounts.json ]]; then
  avidbackup init
fi

echo ""
echo "Install complete."
echo "  Start UI:   source .venv/bin/activate && avidbackup ui"
echo "  Or run:     avidbackup run"
echo "  Schedule:   ./scripts/install_cron.sh"
echo "  Uninstall:  crontab -e  # remove the AvidBackup line"
echo "  Docs:       docs/INSTALL.md"
