"""Start/stop helpers for per-account backup jobs."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from avidbackup.history import HistoryStore
from avidbackup.locks import pid_alive

log = logging.getLogger(__name__)


def read_run_lock_pid(backup_root: Path) -> int | None:
    lock_path = backup_root / ".backup_run.lock"
    if not lock_path.is_file():
        return None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip().splitlines()
        return int(raw[0]) if raw else None
    except (OSError, ValueError):
        return None


def stop_account_backup(backup_root: Path, *, account_id: str, history_db: Path) -> tuple[bool, str]:
    """
    Stop a running account backup process.

    Returns (ok, message). Marks open history rows interrupted when possible.
    """
    pid = read_run_lock_pid(backup_root)
    if pid is None:
        return False, "No backup lock found — nothing to stop."
    if not pid_alive(pid):
        # Stale lock — close history and remove lock.
        try:
            HistoryStore(history_db).close_stale_runs(account_id=account_id, hours=0)
        except Exception:  # noqa: BLE001
            pass
        try:
            (backup_root / ".backup_run.lock").unlink(missing_ok=True)
        except OSError:
            pass
        return True, f"Cleared stale lock (pid {pid} not running)."

    try:
        if sys.platform == "win32":
            # /T kills child tree (python + helpers); /F force.
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode not in {0, 128, 255}:
                # 128 = process not found on some Windows builds
                err = (proc.stderr or proc.stdout or "").strip()
                if "not found" not in err.lower():
                    return False, f"Stop failed for pid {pid}: {err or proc.returncode}"
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"Could not stop pid {pid}: {exc}"

    try:
        HistoryStore(history_db).close_stale_runs(account_id=account_id, hours=0)
    except Exception:  # noqa: BLE001
        log.exception("Could not mark interrupted after stop for %s", account_id)

    # Lock file is normally released by the dying process atexit; force-clear if needed.
    import time

    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    try:
        if not pid_alive(pid):
            (backup_root / ".backup_run.lock").unlink(missing_ok=True)
    except OSError:
        pass

    return True, f"Stopped backup for {account_id} (pid {pid})."
