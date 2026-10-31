from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from avidbackup.config import Settings
from avidbackup.manifest import FileState, Manifest
from avidbackup.progress import ProgressReporter, QueueItem
from avidbackup.remote import RemoteClient, connect

log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    scanned: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    retries: int = 0


def _should_skip(remote_path: str, skip_paths: list[str]) -> bool:
    lowered = remote_path.lower().replace("\\", "/")
    return any(token.lower() in lowered for token in skip_paths)


def _remote_to_local(remote_root: str, remote_path: str, local_root: Path) -> Path:
    root = PurePosixPath(remote_root if remote_root != "/" else "/")
    path = PurePosixPath(remote_path)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = PurePosixPath(path.name)
    return local_root.joinpath(*rel.parts)


def _download_with_retries(
    settings: Settings,
    remote_path: str,
    local_path: Path,
    size: int,
    mtime: float | None,
    retries: int,
    progress: ProgressReporter | None,
) -> tuple[bool, int]:
    """
    Download one file with up to `retries` attempts.
    Reconnects FTPS on each retry (SSL EOF kills the session).
    Returns (ok, attempts_used_after_first).
    """
    attempts = max(1, retries)
    last_exc: Exception | None = None
    extra_retries = 0
    for attempt in range(1, attempts + 1):
        try:
            if progress is not None:
                progress.begin_file(remote_path, size, attempt, attempts)
            with connect(settings) as client:
                log.info(
                    "Downloading %s (%s bytes)%s",
                    remote_path,
                    size,
                    f" [try {attempt}/{attempts}]" if attempt > 1 else "",
                )
                cb = progress.on_bytes if progress is not None else None
                client.download_file(remote_path, local_path, progress=cb)
            if mtime is not None:
                try:
                    os.utime(local_path, (mtime, mtime))
                except OSError:
                    pass
            return True, extra_retries
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts:
                extra_retries += 1
                wait = min(30, 2 * attempt)
                log.warning(
                    "Retry %s/%s for %s after error: %s (sleep %ss)",
                    attempt,
                    attempts,
                    remote_path,
                    exc,
                    wait,
                )
                if progress is not None:
                    progress.status.message = f"Retry {attempt}/{attempts} after error — sleep {wait}s"
                    progress.update_recent_status(
                        remote_path, "transferring", attempt=attempt, error=str(exc)
                    )
                    progress._write(force=True)  # noqa: SLF001
                time.sleep(wait)
            else:
                log.error("Failed %s after %s tries: %s", remote_path, attempts, exc)
    if last_exc:
        raise last_exc
    return False, extra_retries


def sync_changed_files(
    settings: Settings,
    client: RemoteClient | None = None,
    *,
    account_id: str = "",
) -> SyncStats:
    """
    Download new/changed files only, with automatic retries (default 3).

    Local copies are NEVER deleted when a file is removed on the server.
    Live status is written to .transfer_status.json for the management UI.
    """
    if settings.prune_local:
        log.warning(
            "PRUNE_LOCAL=true is set — this utility still will not delete local files; "
            "prune is intentionally unsupported so backups remain forever."
        )

    settings.local_files_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(settings.manifest_path)
    manifest.load()

    stats = SyncStats()
    retries = max(1, int(getattr(settings, "download_retries", 3) or 3))
    aid = account_id or settings.local_backup_root.name
    progress = ProgressReporter(
        settings.local_backup_root / ".transfer_status.json",
        account_id=aid,
    )
    progress.set_phase("scanning", "Connecting and scanning remote tree…")

    log.info(
        "Scanning remote files under %s (never-delete local archive, retries=%s)",
        settings.remote_files_root,
        retries,
    )

    listing_client = client
    close_listing = False
    if listing_client is None:
        listing_client = connect(settings)
        listing_client.__enter__()
        close_listing = True

    try:
        entries = []
        for i, entry in enumerate(listing_client.walk_files(settings.remote_files_root), start=1):
            entries.append(entry)
            # Update often so the UI does not look idle during a long remote walk
            if i == 1 or i % 10 == 0:
                progress.set_scan(i)
        progress.set_scan(len(entries))
    finally:
        if close_listing and listing_client is not None:
            try:
                listing_client.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    queue: list[QueueItem] = []
    for entry in entries:
        stats.scanned += 1
        if _should_skip(entry.path, settings.skip_paths):
            stats.skipped += 1
            continue

        local_path = _remote_to_local(
            settings.remote_files_root, entry.path, settings.local_files_dir
        )

        if not manifest.needs_download(
            entry.path, entry.size, entry.mtime, local_path, settings.incremental
        ):
            stats.skipped += 1
            continue

        queue.append(QueueItem(path=entry.path, size=entry.size, status="queued"))

    if settings.max_files and settings.max_files > 0:
        queue = queue[: settings.max_files]

    progress.status.skipped = stats.skipped
    progress.set_queue(queue)

    # Map path → RemoteEntry for mtime
    by_path = {e.path: e for e in entries}

    for item in queue:
        entry = by_path[item.path]
        local_path = _remote_to_local(
            settings.remote_files_root, entry.path, settings.local_files_dir
        )
        try:
            ok, extra = _download_with_retries(
                settings,
                entry.path,
                local_path,
                entry.size,
                entry.mtime,
                retries,
                progress,
            )
            stats.retries += extra
            if not ok:
                stats.failed += 1
                progress.finish_file(False, entry.size, error="download failed", retries_used=extra)
                continue
            manifest.set(
                entry.path,
                FileState(size=entry.size, mtime=entry.mtime, local_path=str(local_path)),
            )
            stats.downloaded += 1
            stats.bytes += entry.size
            progress.finish_file(True, entry.size, retries_used=extra)
            if stats.downloaded % 25 == 0:
                manifest.save()
        except Exception as exc:  # noqa: BLE001
            stats.failed += 1
            log.error("Failed %s: %s", entry.path, exc)
            progress.finish_file(False, entry.size, error=str(exc), retries_used=0)

    manifest.save()
    progress.finish(
        failed=stats.failed > 0,
        message=(
            f"Done — downloaded={stats.downloaded} skipped={stats.skipped} "
            f"failed={stats.failed} retries={stats.retries}"
        ),
    )
    log.info(
        "Files sync done — scanned=%s downloaded=%s skipped=%s failed=%s retries=%s bytes=%s",
        stats.scanned,
        stats.downloaded,
        stats.skipped,
        stats.failed,
        stats.retries,
        stats.bytes,
    )
    return stats

