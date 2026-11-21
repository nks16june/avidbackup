from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avidbackup.locks import pid_alive


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def interrupt_reason(*, pid: int | None, account_id: str = "", hours: int | None = None) -> str:
    if pid is not None and not pid_alive(pid):
        who = f" for {account_id}" if account_id else ""
        return (
            f"Interrupted{who}: process {pid} stopped before the backup finished. "
            "Usual causes: Task Scheduler stopped/killed the job, the PC rebooted or slept, "
            "power loss, or a crash. Open the account log under logs/ and fix the cause."
        )
    if hours is not None:
        who = f" for {account_id}" if account_id else ""
        return (
            f"Interrupted{who}: still marked running after {hours} hours with no finish. "
            "The job was likely stuck or killed without updating history."
        )
    who = f" for {account_id}" if account_id else ""
    return f"Interrupted{who}: run stopped before it finished."


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
            if "notify_sent" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN notify_sent INTEGER DEFAULT 0")

    def start_run(self, *, account_id: str = "", detail: dict[str, Any] | None = None) -> int:
        payload = dict(detail or {})
        payload.setdefault("pid", os.getpid())
        if account_id:
            payload.setdefault("account_id", account_id)
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status, detail_json) VALUES (?, ?, ?)",
                (_utc_now(), "running", json.dumps(payload)),
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
        sent = 1 if notify_sent else 0
        with self._conn() as conn:
            row = conn.execute(
                "SELECT detail_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            merged: dict[str, Any] = {}
            if row and row["detail_json"]:
                try:
                    merged = json.loads(row["detail_json"])
                except (TypeError, json.JSONDecodeError):
                    merged = {}
            if detail:
                merged.update(detail)
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
                    sent,
                    error,
                    json.dumps(merged),
                    run_id,
                ),
            )

    def close_stale_runs(self, *, hours: int = 12, account_id: str = "") -> int:
        """
        Mark abandoned 'running' rows with a clear reason.

        If the recorded PID is already dead, close immediately. Otherwise wait
        until `hours` have passed so a long healthy dump is not cut short.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        closed = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, started_at, detail_json, error FROM runs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                started = datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                try:
                    detail = json.loads(row["detail_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    detail = {}
                pid = detail.get("pid")
                try:
                    pid_i = int(pid) if pid is not None else None
                except (TypeError, ValueError):
                    pid_i = None
                aid = str(detail.get("account_id") or account_id or "")
                dead = pid_i is not None and not pid_alive(pid_i)
                too_old = started.timestamp() <= cutoff
                if not dead and not too_old:
                    continue
                reason = interrupt_reason(
                    pid=pid_i if dead else None,
                    account_id=aid,
                    hours=None if dead else hours,
                )
                conn.execute(
                    """
                    UPDATE runs
                    SET finished_at=?, status='interrupted', error=?
                    WHERE id=? AND status='running'
                    """,
                    (_utc_now(), reason, row["id"]),
                )
                closed += 1
        return closed

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

