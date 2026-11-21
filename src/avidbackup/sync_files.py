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
    warnings: int = 0
    # Bounded lists for the Live "what finished / what didn't" report.
    downloaded_paths: list[str] | None = None
    skipped_paths: list[str] | None = None
    failed_paths: list[str] | None = None
    left_out_paths: list[str] | None = None

    def __post_init__(self) -> None:
        if self.downloaded_paths is None:
            self.downloaded_paths = []
        if self.skipped_paths is None:
            self.skipped_paths = []
        if self.failed_paths is None:
            self.failed_paths = []
        if self.left_out_paths is None:
            self.left_out_paths = []


_REPORT_CAP = 400


def _note(paths: list[str], path: str) -> None:
    if len(paths) < _REPORT_CAP:
        paths.append(path)


def write_file_report(account_root: Path, stats: SyncStats, *, queue_left: list[str] | None = None) -> Path:
    """Persist last file sync breakdown for the Live console."""
    import json
    from datetime import datetime, timezone

    path = account_root / ".last_file_report.json"
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scanned": stats.scanned,
        "downloaded": stats.downloaded,
        "skipped": stats.skipped,
        "failed": stats.failed,
        "warnings": stats.warnings,
        "bytes": stats.bytes,
        "downloaded_paths": list(stats.downloaded_paths or [])[:_REPORT_CAP],
        "skipped_sample": list(stats.skipped_paths or [])[: min(80, _REPORT_CAP)],
        "failed_paths": list(stats.failed_paths or [])[:_REPORT_CAP],
        "left_out_paths": list(stats.left_out_paths or [])[:_REPORT_CAP],
        "still_queued": list(queue_left or [])[:_REPORT_CAP],
        "note": (
            "skipped = already on disk (size+mtime match). "
            "left_out = server refused (e.g. FTP 550). "
            "still_queued = not attempted yet (interrupted) — Backup/Resume continues these."
        ),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def is_permanent_file_miss(exc: BaseException) -> bool:
    """FTP 550: the server will not give us this one file. Retrying will not help."""
    text = str(exc).lstrip()
    return text.startswith("550") or " 550 " in f" {text}"


# Dot names we still want in backups (site config / ACME). Everything else that
# starts with "." is treated as cPanel/home noise (.cpanel, .trash, .caldav, …).
_DOT_KEEP_EXACT = {
    ".htaccess",
    ".htpasswd",
    ".user.ini",
    ".well-known",
    ".gitignore",
}
_DOT_KEEP_PREFIXES = (".env",)  # .env, .env_local, .env.production, …


def _should_skip(remote_path: str, skip_paths: list[str]) -> bool:
    """
    Skip when a path *segment* matches a skip name, or is a dotted cPanel/home
    folder (``.cpanel``, ``.trash``, …). Keep ``app`` / ``upload`` / ``storage``
    / ``public_html``, and allowlisted dot config (``.env``, ``.htaccess``, …).
    """
    parts = [p for p in remote_path.replace("\\", "/").lower().split("/") if p]
    tokens = {t.lower().strip("/") for t in skip_paths if t and t.strip()}
    for part in parts:
        if part in tokens:
            return True
        if part.startswith("."):
            if part in _DOT_KEEP_EXACT:
                continue
            if any(part.startswith(prefix) for prefix in _DOT_KEEP_PREFIXES):
                continue
            return True
    return False


def _remote_to_local(remote_root: str, remote_path: str, local_root: Path) -> Path:
    """
    Map a remote absolute path to the local files tree.

    Always keeps the **exact server hierarchy** from the FTP account home
    (``/public_html/...``, ``/app/...``, ``/upload/...``). ``remote_root`` only
    controls what we scan — it must not flatten ``public_html`` away, or home
    folders collide with web folders in the same local directory.
    """
    del remote_root  # scan root only; storage is always account-home absolute
    path = PurePosixPath(remote_path)
    parts = [p for p in path.parts if p and p != "/"]
    if not parts:
        return local_root
    return local_root.joinpath(*parts)


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
            if is_permanent_file_miss(exc):
                log.warning("Server refused %s: %s", remote_path, exc)
                raise
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
    progress.set_phase("scanning", "Connecting — will scan folder by folder…")

    log.info(
        "Scanning remote files under %s folder-by-folder "
        "(FTP/SFTP, never-delete local archive, retries=%s)",
        settings.remote_files_root,
        retries,
    )

    listing_client = client
    close_listing = False
    if listing_client is None:
        listing_client = connect(settings)
        listing_client.__enter__()
        close_listing = True

    entries: list = []
    queue: list[QueueItem] = []
    by_path: dict = {}
    folders_done = 0
    try:
        walk = getattr(listing_client, "walk_folders", None)
        def _skip_dir(path: str) -> bool:
            return _should_skip(path, settings.skip_paths)

        if walk is None:
            # Older clients: flat walk still works
            for i, entry in enumerate(
                listing_client.walk_files(
                    settings.remote_files_root,
                    skip_dir=_skip_dir,
                ),
                start=1,
            ):
                entries.append(entry)
                if i == 1 or i % 10 == 0:
                    progress.set_scan(i)
            progress.set_scan(len(entries))
        else:
            for folder, files in walk(
                settings.remote_files_root,
                skip_dir=_skip_dir,
            ):
                folders_done += 1
                log.debug("Scanning folder %s (%s file(s))", folder, len(files))
                for entry in files:
                    entries.append(entry)
                    stats.scanned += 1
                    by_path[entry.path] = entry
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
                        _note(stats.skipped_paths or [], entry.path)
                        # Remap manifest to today's day-folder path without re-downloading.
                        manifest.set(
                            entry.path,
                            FileState(
                                size=entry.size,
                                mtime=entry.mtime,
                                local_path=str(local_path),
                            ),
                        )
                        continue
                    queue.append(QueueItem(path=entry.path, size=entry.size, status="queued"))
                    if settings.max_files and settings.max_files > 0 and len(queue) >= settings.max_files:
                        break
                if settings.max_files and settings.max_files > 0 and len(queue) >= settings.max_files:
                    break
                # Live skip/queue counts so the UI does not stay at SKIPPED=0 all scan.
                progress.set_scan(
                    len(entries),
                    folder=folder,
                    folders_scanned=folders_done,
                    skipped=stats.skipped,
                    queued=len(queue),
                )
                # Full LIST still runs; throttle INFO to cut log I/O on huge trees.
                if folders_done == 1 or folders_done % 25 == 0:
                    log.info(
                        "Scan progress: %s folder(s), %s file(s), %s unchanged, %s to download",
                        folders_done,
                        len(entries),
                        stats.skipped,
                        len(queue),
                    )
    finally:
        if close_listing and listing_client is not None:
            try:
                listing_client.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    # Flat-walk path: build queue after full listing
    if not by_path and entries:
        for entry in entries:
            stats.scanned += 1
            by_path[entry.path] = entry
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
                _note(stats.skipped_paths or [], entry.path)
                manifest.set(
                    entry.path,
                    FileState(
                        size=entry.size,
                        mtime=entry.mtime,
                        local_path=str(local_path),
                    ),
                )
                continue
            queue.append(QueueItem(path=entry.path, size=entry.size, status="queued"))
        if settings.max_files and settings.max_files > 0:
            queue = queue[: settings.max_files]

    progress.status.skipped = stats.skipped
    # Persist remapped paths before downloads so a kill mid-transfer keeps skip state.
    manifest.save()
    progress.set_phase(
        "transferring" if queue else "done",
        f"Scan done — {stats.scanned} listed, {stats.skipped} unchanged, {len(queue)} to download",
    )
    progress.set_queue(queue)

    remaining = [item.path for item in queue]
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
                _note(stats.failed_paths or [], entry.path)
                progress.finish_file(False, entry.size, error="download failed", retries_used=extra)
                if remaining and remaining[0] == entry.path:
                    remaining.pop(0)
                elif entry.path in remaining:
                    remaining.remove(entry.path)
                continue
            manifest.set(
                entry.path,
                FileState(size=entry.size, mtime=entry.mtime, local_path=str(local_path)),
            )
            stats.downloaded += 1
            _note(stats.downloaded_paths or [], entry.path)
            stats.bytes += entry.size
            progress.finish_file(True, entry.size, retries_used=extra)
            if remaining and remaining[0] == entry.path:
                remaining.pop(0)
            elif entry.path in remaining:
                remaining.remove(entry.path)
            if stats.downloaded % 25 == 0:
                manifest.save()
                write_file_report(settings.local_backup_root, stats, queue_left=remaining)
        except Exception as exc:  # noqa: BLE001
            if is_permanent_file_miss(exc):
                stats.warnings += 1
                stats.skipped += 1
                _note(stats.left_out_paths or [], entry.path)
                log.warning(
                    "Left out %s — the server refused to read it (%s). The rest of the backup continues.",
                    entry.path,
                    exc,
                )
                progress.finish_file(False, entry.size, error=str(exc), retries_used=0)
                if remaining and remaining[0] == entry.path:
                    remaining.pop(0)
                elif entry.path in remaining:
                    remaining.remove(entry.path)
                continue
            stats.failed += 1
            _note(stats.failed_paths or [], entry.path)
            log.error("Failed %s: %s", entry.path, exc)
            progress.finish_file(False, entry.size, error=str(exc), retries_used=0)
            if remaining and remaining[0] == entry.path:
                remaining.pop(0)
            elif entry.path in remaining:
                remaining.remove(entry.path)

    manifest.save()
    write_file_report(settings.local_backup_root, stats, queue_left=remaining)
    progress.finish(
        failed=stats.failed > 0,
        message=(
            f"Done — downloaded={stats.downloaded} skipped={stats.skipped} "
            f"failed={stats.failed} warnings={stats.warnings} retries={stats.retries}"
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

