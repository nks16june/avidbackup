"""Read per-account backup log tails for the live UI."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from avidbackup.paths import logs_dir
from avidbackup.validation import validate_account_id

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def account_log_safe_id(account_id: str) -> str:
    validate_account_id(account_id)
    return _SAFE.sub("_", account_id).strip("._-") or "all"


def latest_account_log(account_id: str, *, root: Path | None = None) -> Path | None:
    """Newest backup_YYYYMMDD_<account>.log for this account."""
    safe = account_log_safe_id(account_id)
    folder = logs_dir(root)
    if not folder.is_dir():
        return None
    matches = [
        p
        for p in folder.glob(f"backup_*_{safe}.log")
        if p.is_file() and p.name.startswith("backup_")
    ]
    if not matches:
        # Prefer today's stamp if present even when empty list edge case
        stamp = datetime.now().strftime("%Y%m%d")
        today = folder / f"backup_{stamp}_{safe}.log"
        return today if today.is_file() else None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def read_log_chunk(
    account_id: str,
    *,
    offset: int = 0,
    max_bytes: int = 96_000,
    root: Path | None = None,
) -> dict[str, Any]:
    """
    Return new log bytes from offset (byte position).

    If offset is behind a truncated/rotated file, restarts from max(0, size-max_bytes).
    """
    path = latest_account_log(account_id, root=root)
    if path is None:
        return {
            "account_id": account_id,
            "path": None,
            "name": None,
            "offset": 0,
            "size": 0,
            "text": "",
            "reset": False,
        }

    folder = logs_dir(root).resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(folder)
    except (OSError, ValueError):
        return {
            "account_id": account_id,
            "path": None,
            "name": None,
            "offset": 0,
            "size": 0,
            "text": "",
            "reset": False,
            "error": "log path outside logs dir",
        }

    try:
        size = resolved.stat().st_size
    except OSError:
        size = 0

    start = max(0, int(offset or 0))
    reset = False
    if start > size:
        # File rotated or truncated.
        start = max(0, size - max_bytes)
        reset = True
    elif start == 0 and size > max_bytes:
        # First paint: show the tail only.
        start = size - max_bytes
        reset = True

    text = ""
    new_offset = start
    try:
        with resolved.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(max_bytes)
            new_offset = start + len(raw)
            # Drop partial first line when starting mid-file.
            if start > 0 and raw:
                nl = raw.find(b"\n")
                if 0 <= nl < len(raw) - 1:
                    raw = raw[nl + 1 :]
            text = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return {
            "account_id": account_id,
            "path": str(resolved),
            "name": resolved.name,
            "offset": start,
            "size": size,
            "text": "",
            "reset": reset,
            "error": str(exc),
        }

    return {
        "account_id": account_id,
        "path": str(resolved),
        "name": resolved.name,
        "offset": new_offset,
        "size": size,
        "text": text,
        "reset": reset,
    }
