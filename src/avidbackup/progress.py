from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class QueueItem:
    path: str
    size: int
    status: str = "queued"  # queued | transferring | done | failed | skipped
    attempt: int = 0
    error: str = ""
    transferred: int = 0


@dataclass
class TransferStatus:
    account_id: str = ""
    phase: str = "idle"  # idle | scanning | transferring | done | failed
    message: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    scanned: int = 0
    folders_scanned: int = 0
    queued: int = 0
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    retries: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current_folder: str = ""
    current_path: str = ""
    current_size: int = 0
    current_transferred: int = 0
    current_attempt: int = 0
    current_retries_max: int = 3
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    recent: list[dict[str, Any]] = field(default_factory=list)
    # Rolling samples for the live UI sparkline (client can also append from polls).
    samples: list[dict[str, Any]] = field(default_factory=list)


class ProgressReporter:
    """Writes FileZilla-like live transfer status for the management UI to poll."""

    def __init__(self, path: Path, account_id: str = "") -> None:
        self.path = path
        self.status = TransferStatus(account_id=account_id, started_at=time.time())
        self._last_write = 0.0
        self._speed_t0 = time.time()
        self._speed_b0 = 0
        self._min_interval = 0.2

    def _write(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_write) < self._min_interval:
            return
        self.status.updated_at = now
        elapsed = max(0.001, now - self._speed_t0)
        delta_b = max(0, self.status.bytes_done + self.status.current_transferred - self._speed_b0)
        # Smoothish instantaneous speed over rolling window
        inst = delta_b / elapsed
        if self.status.speed_bps <= 0:
            self.status.speed_bps = inst
        else:
            self.status.speed_bps = (self.status.speed_bps * 0.7) + (inst * 0.3)
        remaining = max(0, self.status.bytes_total - self.status.bytes_done - self.status.current_transferred)
        if self.status.speed_bps > 100:
            self.status.eta_seconds = remaining / self.status.speed_bps
        else:
            self.status.eta_seconds = None
        if elapsed > 1.5:
            self._speed_t0 = now
            self._speed_b0 = self.status.bytes_done + self.status.current_transferred
        # Keep a short throughput history for the graphical live console (~1 Hz).
        last_sample_t = self.status.samples[-1]["t"] if self.status.samples else 0.0
        if (now - float(last_sample_t)) >= 0.9:
            self.status.samples.append(
                {
                    "t": round(now, 2),
                    "bytes": int(self.status.bytes_done + self.status.current_transferred),
                    "bps": round(float(self.status.speed_bps or 0.0), 1),
                    "downloaded": int(self.status.downloaded),
                    "scanned": int(self.status.scanned),
                }
            )
            self.status.samples = self.status.samples[-90:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = asdict(self.status)
        body = json.dumps(payload, ensure_ascii=False)
        # Windows often denies replace while the UI is reading the status file —
        # never crash the backup over a live-status write.
        for attempt in range(8):
            try:
                tmp.write_text(body, encoding="utf-8")
                tmp.replace(self.path)
                break
            except OSError:
                if attempt >= 7:
                    try:
                        self.path.write_text(body, encoding="utf-8")
                    except OSError:
                        return
                    break
                time.sleep(0.05 * (attempt + 1))
        self._last_write = now

    def set_phase(self, phase: str, message: str = "") -> None:
        self.status.phase = phase
        self.status.message = message
        self._write(force=True)

    def set_scan(
        self,
        scanned: int,
        *,
        folder: str = "",
        folders_scanned: int | None = None,
        skipped: int | None = None,
        queued: int | None = None,
    ) -> None:
        self.status.scanned = scanned
        if folder:
            self.status.current_folder = folder
        if folders_scanned is not None:
            self.status.folders_scanned = folders_scanned
        if skipped is not None:
            self.status.skipped = skipped
        if queued is not None:
            self.status.queued = queued
        self.status.phase = "scanning"
        folder_bit = f" · {folder}" if folder else ""
        folders_bit = (
            f"{self.status.folders_scanned} folder(s), "
            if self.status.folders_scanned
            else ""
        )
        skip_bit = f", {self.status.skipped} unchanged" if self.status.skipped else ""
        self.status.message = f"Scanning {folders_bit}{scanned} file(s){skip_bit}{folder_bit}"
        self._write()

    def set_queue(self, items: list[QueueItem]) -> None:
        self.status.queued = len(items)
        self.status.bytes_total = sum(i.size for i in items)
        self.status.recent = [
            {"path": i.path, "size": i.size, "status": i.status, "attempt": i.attempt, "error": i.error}
            for i in items[:40]
        ]
        self.status.phase = "transferring" if items else "done"
        self.status.message = f"{len(items)} file(s) queued"
        self._write(force=True)

    def update_recent_status(self, path: str, status: str, *, attempt: int = 0, error: str = "", transferred: int = 0) -> None:
        for row in self.status.recent:
            if row.get("path") == path:
                row["status"] = status
                row["attempt"] = attempt
                row["error"] = error
                row["transferred"] = transferred
                break
        else:
            self.status.recent.insert(
                0,
                {
                    "path": path,
                    "size": self.status.current_size,
                    "status": status,
                    "attempt": attempt,
                    "error": error,
                    "transferred": transferred,
                },
            )
            self.status.recent = self.status.recent[:40]

    def begin_file(self, path: str, size: int, attempt: int, retries_max: int) -> None:
        self.status.phase = "transferring"
        self.status.current_path = path
        self.status.current_size = size
        self.status.current_transferred = 0
        self.status.current_attempt = attempt
        self.status.current_retries_max = retries_max
        self.status.message = f"Downloading {path}"
        self.update_recent_status(path, "transferring", attempt=attempt)
        self._write(force=True)

    def on_bytes(self, n: int) -> None:
        self.status.current_transferred = n
        self.update_recent_status(
            self.status.current_path,
            "transferring",
            attempt=self.status.current_attempt,
            transferred=n,
        )
        self._write()

    def finish_file(self, ok: bool, size: int, *, error: str = "", retries_used: int = 0) -> None:
        if ok:
            self.status.downloaded += 1
            self.status.bytes_done += size
            self.update_recent_status(self.status.current_path, "done", attempt=self.status.current_attempt)
        else:
            self.status.failed += 1
            self.update_recent_status(
                self.status.current_path,
                "failed",
                attempt=self.status.current_attempt,
                error=error,
            )
        self.status.retries += retries_used
        self.status.current_path = ""
        self.status.current_size = 0
        self.status.current_transferred = 0
        self._write(force=True)

    def bump_skipped(self, n: int = 1) -> None:
        self.status.skipped += n
        self._write()

    def finish(self, *, failed: bool = False, message: str = "") -> None:
        self.status.phase = "failed" if failed else "done"
        self.status.message = message or ("Finished with errors" if failed else "Transfer complete")
        self.status.current_path = ""
        self.status.current_transferred = 0
        self._write(force=True)

