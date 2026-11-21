from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()
_FILE_HANDLERS: set[str] = set()
_CONSOLE_READY = False
_TLS = threading.local()


def set_log_account(account_id: str) -> None:
    """Bind the current thread to one account so its lines stay in its own log file."""
    _TLS.account_id = account_id or "-"


class _FileAccountFilter(logging.Filter):
    def __init__(self, account_id: str) -> None:
        super().__init__()
        self.account_id = account_id

    def filter(self, record: logging.LogRecord) -> bool:
        current = getattr(_TLS, "account_id", None) or "-"
        record.account_id = current
        return current == self.account_id


class _ConsoleAccountFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.account_id = getattr(_TLS, "account_id", None) or getattr(
            record, "account_id", "-"
        )
        return True


def setup_logging(log_dir: Path, level: str = "INFO", *, account_id: str = "") -> Path:
    """
    Attach a daily per-account log file. Safe for parallel runs: each file only
    receives lines from that account's thread.
    """
    global _CONSOLE_READY
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", account_id).strip("._-") or "all"
    log_file = log_dir / f"backup_{stamp}_{safe}.log"
    account = account_id or "all"
    set_log_account(account)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(account_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    with _LOCK:
        key = str(log_file.resolve())
        if key not in _FILE_HANDLERS:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(_FileAccountFilter(account))
            root.addHandler(fh)
            _FILE_HANDLERS.add(key)
        if not _CONSOLE_READY:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            sh.addFilter(_ConsoleAccountFilter())
            root.addHandler(sh)
            _CONSOLE_READY = True

    return log_file
