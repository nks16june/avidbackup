from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_app_lock(home: Path) -> Path:
    """
    Single-instance lock for `avidbackup app` / desktop UI.
    Raises RuntimeError if another live process holds the lock.
    """
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / ".avidbackup_app.lock"
    if lock_path.is_file():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip().splitlines()[0])
        except (ValueError, IndexError):
            old_pid = 0
        if _pid_alive(old_pid) and old_pid != os.getpid():
            raise RuntimeError(
                f"AvidBackup app already running (pid={old_pid}). "
                "Open the existing window/tray or quit that process first."
            )
        log.info("Removing stale app lock (pid %s gone)", old_pid)
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _release() -> None:
        try:
            if lock_path.is_file():
                cur = lock_path.read_text(encoding="utf-8").splitlines()
                if cur and cur[0].strip() == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_release)
    return lock_path


def acquire_run_lock(backup_root: Path, account_id: str) -> None:
    """Exclusive per-account run lock. Raises RuntimeError if another live process holds it."""
    backup_root.mkdir(parents=True, exist_ok=True)
    lock_path = backup_root / ".backup_run.lock"
    if lock_path.is_file():
        try:
            raw = lock_path.read_text(encoding="utf-8").strip().splitlines()
            old_pid = int(raw[0]) if raw else 0
        except ValueError:
            old_pid = 0
        still_alive = _pid_alive(old_pid)
        if still_alive and old_pid != os.getpid():
            raise RuntimeError(
                f"Account {account_id} already has a backup running (pid={old_pid}). "
                "Stop it or wait before starting another."
            )
        log.info("Removing stale lock for %s (pid %s gone)", account_id, old_pid)
    lock_path.write_text(f"{os.getpid()}\n{account_id}\n", encoding="utf-8")

    def _release() -> None:
        try:
            if lock_path.is_file():
                cur = lock_path.read_text(encoding="utf-8").splitlines()
                if cur and cur[0].strip() == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_release)
