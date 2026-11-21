"""Push a local day snapshot's files back to the account over FTP/SFTP."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from avidbackup.config import Settings
from avidbackup.remote import RemoteClient, connect

log = logging.getLogger(__name__)


@dataclass
class FtpRestoreStats:
    uploaded: int = 0
    failed: int = 0
    bytes: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


@dataclass
class RestorePlan:
    """What a restore would upload. Built locally; nothing is sent to the server."""

    files: int
    bytes: int
    truncated: bool
    sample: list[str]


def _resolve_start(root: Path, rel: str) -> Path:
    if not rel:
        return root
    candidate = (root / rel.replace("\\", "/").strip("/")).resolve()
    candidate.relative_to(root)  # raises ValueError if it escapes the snapshot
    if not candidate.is_dir() and not candidate.is_file():
        raise FileNotFoundError(rel)
    return candidate


def _iter_upload_paths(start: Path):
    if start.is_file():
        yield start
        return
    for dirpath, _dirs, filenames in os.walk(start):
        for name in filenames:
            if name.startswith(".") or name.endswith(".part"):
                continue
            yield Path(dirpath) / name


def plan_restore(
    files_dir: Path, *, rel: str = "", limit: int = 500_000, sample_size: int = 8
) -> RestorePlan:
    """Count what would be uploaded (local only) so the user can review before confirming."""
    root = files_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"No files folder: {files_dir}")
    start = _resolve_start(root, rel)
    files = 0
    total = 0
    sample: list[str] = []
    truncated = False
    for path in _iter_upload_paths(start):
        files += 1
        try:
            total += path.stat().st_size
        except OSError:
            pass
        if len(sample) < sample_size:
            sample.append(_local_rel_to_remote(path.relative_to(root).as_posix()))
        if files >= limit:
            truncated = True
            break
    return RestorePlan(files=files, bytes=total, truncated=truncated, sample=sample)


def _local_rel_to_remote(rel: str) -> str:
    """files/public_html/x → /public_html/x (exact account-home hierarchy)."""
    clean = rel.replace("\\", "/").strip("/")
    return f"/{clean}" if clean else "/"


def restore_day_files_via_ftp(
    settings: Settings,
    files_dir: Path,
    *,
    rel: str = "",
    client: RemoteClient | None = None,
    max_files: int = 0,
) -> FtpRestoreStats:
    """
    Upload local day ``files/`` (or a subfolder) back to the FTP account.

    Local layout is the account-home tree (``public_html/...``, ``app/...``).
    """
    stats = FtpRestoreStats()
    root = files_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"No files folder: {files_dir}")

    start = _resolve_start(root, rel)

    own = client is None
    remote = client
    if own:
        remote = connect(settings)
        remote.__enter__()
    assert remote is not None

    try:
        paths: list[Path] = []
        for path in _iter_upload_paths(start):
            paths.append(path)
            if max_files and len(paths) >= max_files:
                break

        for local in paths:
            rel_path = local.relative_to(root).as_posix()
            remote_path = _local_rel_to_remote(rel_path)
            try:
                size = local.stat().st_size
                remote.upload_file(local, remote_path)
                stats.uploaded += 1
                stats.bytes += size
                if stats.uploaded % 50 == 0:
                    log.info("FTP restore progress: %s file(s)", stats.uploaded)
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                msg = f"{remote_path}: {exc}"
                stats.errors.append(msg)
                log.error("FTP restore failed for %s: %s", remote_path, exc)
    finally:
        if own and remote is not None:
            try:
                remote.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    return stats
