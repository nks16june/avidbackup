from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from avidbackup.config import Settings

MARKER_NAME = ".initial_full_done.json"


def marker_path(settings: Settings) -> Path:
    return settings.local_backup_root / MARKER_NAME


def has_completed_initial_full(settings: Settings) -> bool:
    """True only after at least one zero-failure full file sync for this account."""
    return marker_path(settings).is_file()


def mark_initial_full_done(
    settings: Settings,
    *,
    account_id: str,
    downloaded: int,
    skipped: int,
    bytes_copied: int,
) -> Path:
    path = marker_path(settings)
    payload = {
        "account_id": account_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "downloaded": downloaded,
        "skipped": skipped,
        "bytes": bytes_copied,
        "note": "Initial full file backup completed with zero failures. Later runs are incremental.",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def clear_initial_full_marker(settings: Settings) -> None:
    path = marker_path(settings)
    if path.is_file():
        path.unlink()

