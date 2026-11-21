from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) raises on access-denied for live processes on Windows,
        # which made us treat healthy backups as dead and mark them interrupted.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


def pid_alive(pid: int) -> bool:
    return _pid_alive(pid)


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


def acquire_run_lock(backup_root: Path, account_id: str) -> Callable[[], None]:
    """
    Exclusive per-account lock across threads and processes.

    Returns a release callback. Call it when the account backup finishes so
    another run of the same account can start; other accounts are never blocked.
    """
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(account_id, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        raise RuntimeError(
            f"Account {account_id} already has a backup running in this process. "
            "One account never starts twice at the same time."
        )

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
            thread_lock.release()
            raise RuntimeError(
                f"Account {account_id} already has a backup running (pid={old_pid}). "
                "That run continues on its own; this start was skipped."
            )
        log.info("Removing stale lock for %s (pid %s gone)", account_id, old_pid)

    lock_path.write_text(
        f"{os.getpid()}\n{account_id}\n{threading.get_ident()}\n",
        encoding="utf-8",
    )

    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        try:
            if lock_path.is_file():
                cur = lock_path.read_text(encoding="utf-8").splitlines()
                if cur and cur[0].strip() == str(os.getpid()):
                    # Only remove if this thread still owns the file.
                    if len(cur) < 3 or cur[2].strip() == str(threading.get_ident()):
                        lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        if thread_lock.locked():
            try:
                thread_lock.release()
            except RuntimeError:
                pass

    atexit.register(release)
    return release
