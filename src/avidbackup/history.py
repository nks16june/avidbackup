from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    files_downloaded INTEGER DEFAULT 0,
                    files_skipped INTEGER DEFAULT 0,
                    files_failed INTEGER DEFAULT 0,
                    files_bytes INTEGER DEFAULT 0,
                    mysql_downloaded INTEGER DEFAULT 0,
                    mysql_bytes INTEGER DEFAULT 0,
                    mysql_path TEXT,
                    mysql_config_source TEXT,
                    notify_sent INTEGER DEFAULT 0,
                    error TEXT,
                    detail_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
                """
            )
            cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            # Rename legacy notify column if present
            if "whatsapp_sent" in cols and "notify_sent" not in cols:
                conn.execute("ALTER TABLE runs RENAME COLUMN whatsapp_sent TO notify_sent")
            elif "notify_sent" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN notify_sent INTEGER DEFAULT 0")

    def start_run(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, ?)",
                (_utc_now(), "running"),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        files_downloaded: int = 0,
        files_skipped: int = 0,
        files_failed: int = 0,
        files_bytes: int = 0,
        mysql_downloaded: int = 0,
        mysql_bytes: int = 0,
        mysql_path: str | None = None,
        mysql_config_source: str | None = None,
        notify_sent: bool = False,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    finished_at=?, status=?,
                    files_downloaded=?, files_skipped=?, files_failed=?, files_bytes=?,
                    mysql_downloaded=?, mysql_bytes=?, mysql_path=?, mysql_config_source=?,
                    notify_sent=?, error=?, detail_json=?
                WHERE id=?
                """,
                (
                    _utc_now(),
                    status,
                    files_downloaded,
                    files_skipped,
                    files_failed,
                    files_bytes,
                    mysql_downloaded,
                    mysql_bytes,
                    mysql_path,
                    mysql_config_source,
                    1 if notify_sent else 0,
                    error,
                    json.dumps(detail or {}),
                    run_id,
                ),
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_run(self) -> dict[str, Any] | None:
        rows = self.list_runs(1)
        return rows[0] if rows else None

    def summary(self, files_root: Path, mysql_root: Path) -> dict[str, Any]:
        file_count = 0
        file_bytes = 0
        if files_root.is_dir():
            for p in files_root.rglob("*"):
                if p.is_file():
                    file_count += 1
                    file_bytes += p.stat().st_size
        mysql_count = 0
        mysql_bytes = 0
        latest_mysql = None
        if mysql_root.is_dir():
            dumps = sorted(
                [p for p in mysql_root.glob("*.sql*") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            mysql_count = len(dumps)
            mysql_bytes = sum(p.stat().st_size for p in dumps)
            if dumps:
                latest_mysql = {
                    "name": dumps[0].name,
                    "size": dumps[0].stat().st_size,
                    "mtime": datetime.fromtimestamp(
                        dumps[0].stat().st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
        return {
            "file_count": file_count,
            "file_bytes": file_bytes,
            "mysql_count": mysql_count,
            "mysql_bytes": mysql_bytes,
            "latest_mysql": latest_mysql,
            "latest_run": self.latest_run(),
        }

