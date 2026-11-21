"""Track in-flight backup runs so a process exit can leave a clear interrupt reason."""

from __future__ import annotations

import atexit
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ACTIVE: dict[tuple[str, int], str] = {}  # (history_db, run_id) -> account_id
_ATEXIT_REGISTERED = False


def register_active_run(history_db: Path, run_id: int, account_id: str) -> None:
    global _ATEXIT_REGISTERED
    key = (str(history_db.resolve()), int(run_id))
    with _LOCK:
        _ACTIVE[key] = account_id
        if not _ATEXIT_REGISTERED:
            atexit.register(_mark_open_runs_on_exit)
            _ATEXIT_REGISTERED = True


def unregister_active_run(history_db: Path, run_id: int) -> None:
    key = (str(history_db.resolve()), int(run_id))
    with _LOCK:
        _ACTIVE.pop(key, None)


def _mark_open_runs_on_exit() -> None:
    from avidbackup.history import HistoryStore

    with _LOCK:
        items = list(_ACTIVE.items())
        _ACTIVE.clear()
    for (db_path, run_id), account_id in items:
        try:
            HistoryStore(Path(db_path)).finish_run(
                run_id,
                status="interrupted",
                error=(
                    f"Interrupted: backup process exited while {account_id} was still running. "
                    "Usual causes: Task Scheduler stopped the job, the PC rebooted/slept, "
                    "power loss, or the process crashed. Check logs/"
                    f"backup_*_{account_id}.log"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not mark interrupted run %s for %s: %s", run_id, account_id, exc)
