#!/usr/bin/env bash
# Install a daily cron job (02:00) for AvidBackup on macOS/Linux
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$ROOT/scripts/run_backup.sh"
chmod +x "$RUNNER" "$ROOT/scripts/install.sh" "$ROOT/scripts/install_cron.sh" || true
mkdir -p "$ROOT/logs"
LINE="0 2 * * * cd \"$ROOT\" && \"$RUNNER\" run >> \"$ROOT/logs/cron.log\" 2>&1"
(crontab -l 2>/dev/null | grep -v 'avidbackup\|run_backup.sh' || true; echo "$LINE") | crontab -
echo "Cron installed: daily 02:00"
echo "$LINE"
echo "Uninstall: crontab -e  # remove the line above"
