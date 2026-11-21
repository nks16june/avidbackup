"""Live transfer payload for the management UI (poll every ~1s)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from avidbackup.history import HistoryStore
from avidbackup.locks import pid_alive
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import AccountConfig, AppConfig

# If .transfer_status.json is older than this and no lock holds, treat as idle/stale.
STALE_SECONDS = 45.0


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _lock_info(data_dir: Path) -> dict[str, Any]:
    lock_path = data_dir / ".backup_run.lock"
    if not lock_path.is_file():
        return {"held": False, "pid": None, "alive": False}
    try:
        raw = lock_path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(raw[0]) if raw else None
    except (OSError, ValueError):
        return {"held": True, "pid": None, "alive": False}
    alive = bool(pid is not None and pid_alive(pid))
    return {"held": alive, "pid": pid, "alive": alive}


def _latest_run(history_db: Path) -> dict[str, Any] | None:
    if not history_db.is_file():
        return None
    rows = HistoryStore(history_db).list_runs(limit=1)
    return rows[0] if rows else None


def build_live_status(cfg: AppConfig, account: AccountConfig) -> dict[str, Any]:
    """Snapshot used by /accounts/<id> and /accounts/<id>/live.json."""
    settings = settings_from_account(cfg, account)
    data_dir = settings.local_backup_root
    # Close abandoned "running" rows so the UI does not show a ghost run.
    try:
        HistoryStore(settings.history_db).close_stale_runs(account_id=account.id, hours=1)
    except Exception:  # noqa: BLE001
        pass
    transfer = _read_json(data_dir / ".transfer_status.json") or {}
    lock = _lock_info(data_dir)
    run = _latest_run(settings.history_db)

    now = time.time()
    updated = float(transfer.get("updated_at") or 0.0)
    age = (now - updated) if updated else None
    phase = str(transfer.get("phase") or "idle")
    fresh = age is not None and age <= STALE_SECONDS
    active_phase = phase in {"scanning", "transferring"}
    live = bool(lock["held"] or (active_phase and fresh))

    if not live and active_phase and not fresh:
        phase = "stale"
    elif not live and phase in {"scanning", "transferring"}:
        phase = "idle"

    bytes_done = int(transfer.get("bytes_done") or 0) + int(transfer.get("current_transferred") or 0)
    bytes_total = int(transfer.get("bytes_total") or 0)
    queued = int(transfer.get("queued") or 0)
    downloaded = int(transfer.get("downloaded") or 0)
    failed = int(transfer.get("failed") or 0)
    skipped = int(transfer.get("skipped") or 0)
    scanned = int(transfer.get("scanned") or 0)
    files_done = downloaded + failed
    # Prefer byte progress when we know the queue size; else file-count progress.
    if bytes_total > 0:
        pct = min(100.0, 100.0 * bytes_done / bytes_total)
    elif queued > 0:
        pct = min(100.0, 100.0 * files_done / queued)
    elif phase in {"done", "failed"} and scanned:
        pct = 100.0
    else:
        pct = 0.0

    return {
        "account_id": account.id,
        "label": account.label,
        "live": live,
        "phase": phase if live or phase in {"done", "failed", "stale", "idle"} else "idle",
        "message": transfer.get("message") or "",
        "updated_at": updated or None,
        "age_seconds": round(age, 1) if age is not None else None,
        "started_at": transfer.get("started_at"),
        "scanned": scanned,
        "folders_scanned": int(transfer.get("folders_scanned") or 0),
        "current_folder": transfer.get("current_folder") or "",
        "queued": queued,
        "downloaded": downloaded,
        "failed": failed,
        "skipped": skipped,
        "retries": int(transfer.get("retries") or 0),
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "pct": round(pct, 2),
        "speed_bps": float(transfer.get("speed_bps") or 0.0),
        "eta_seconds": transfer.get("eta_seconds"),
        "current_path": transfer.get("current_path") or "",
        "current_size": int(transfer.get("current_size") or 0),
        "current_transferred": int(transfer.get("current_transferred") or 0),
        "current_attempt": int(transfer.get("current_attempt") or 0),
        "current_retries_max": int(transfer.get("current_retries_max") or 3),
        "recent": transfer.get("recent") or [],
        "samples": transfer.get("samples") or [],
        "lock": lock,
        "run": {
            "id": run.get("id") if run else None,
            "status": run.get("status") if run else None,
            "started_at": run.get("started_at") if run else None,
            "finished_at": run.get("finished_at") if run else None,
            "error": (run.get("error") or "")[:400] if run else "",
            "files_downloaded": run.get("files_downloaded") if run else 0,
            "files_skipped": run.get("files_skipped") if run else 0,
            "files_failed": run.get("files_failed") if run else 0,
            "mysql_downloaded": run.get("mysql_downloaded") if run else 0,
            "detail": _parse_detail(run.get("detail_json") if run else None),
        }
        if run
        else None,
        "file_report": _read_json(data_dir / ".last_file_report.json"),
        "data_dir": str(data_dir),
        "resume_hint": (
            "Backup/Resume skips files already on disk (matching size + timestamp). "
            "Only new, changed, or previously failed files download again."
        ),
    }


def _parse_detail(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
