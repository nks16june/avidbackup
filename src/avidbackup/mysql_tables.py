from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from avidbackup.config import Settings
from avidbackup.mysql_config import MysqlCredentials
from avidbackup.secure_cli import mysql_defaults_file
from avidbackup.util_bin import find_mysql

log = logging.getLogger(__name__)

DEFAULT_LARGE_TABLE_MB = 50.0


@dataclass
class TableSizeRow:
    table: str
    engine: str
    rows_approx: int
    data_bytes: int
    index_bytes: int
    total_bytes: int
    large: bool


@dataclass
class TableSizeReport:
    database: str
    host: str
    generated_at: str
    large_threshold_bytes: int
    tables: list[TableSizeRow]
    large_tables: list[TableSizeRow]

    @property
    def path_hint(self) -> str:
        return f"{len(self.large_tables)} large / {len(self.tables)} total"


def _query_via_cli(creds: MysqlCredentials, sql: str) -> list[dict[str, str]] | None:
    exe = find_mysql()
    if not exe:
        return None
    try:
        with mysql_defaults_file(
            host=creds.host,
            port=creds.port,
            user=creds.user,
            password=creds.password,
        ) as defaults:
            cmd = [
                exe,
                f"--defaults-extra-file={defaults}",
                "--batch",
                "--raw",
                "--skip-column-names",
                "-e",
                sql,
            ]
            # Never log the command — defaults file path is ok, password is not on argv
            proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[:400]
            log.warning("mysql table-size query failed: %s", err)
            return None
        rows: list[dict[str, str]] = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            rows.append(
                {
                    "table": parts[0],
                    "engine": parts[1] or "",
                    "rows": parts[2] or "0",
                    "data": parts[3] or "0",
                    "index": parts[4] or "0",
                }
            )
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("mysql CLI table sizes error: %s", exc)
        return None


def _query_via_pymysql(creds: MysqlCredentials, sql: str) -> list[dict[str, str]] | None:
    try:
        import pymysql
    except ImportError:
        return None
    try:
        conn = pymysql.connect(
            host=creds.host,
            port=creds.port,
            user=creds.user,
            password=creds.password,
            database=creds.database,
            charset="utf8mb4",
            connect_timeout=30,
            read_timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("PyMySQL table sizes connect failed: %s", exc)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = []
            for r in cur.fetchall():
                rows.append(
                    {
                        "table": str(r[0]),
                        "engine": str(r[1] or ""),
                        "rows": str(r[2] or 0),
                        "data": str(r[3] or 0),
                        "index": str(r[4] or 0),
                    }
                )
            return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("PyMySQL table sizes query failed: %s", exc)
        return None
    finally:
        conn.close()


def collect_table_sizes(
    creds: MysqlCredentials,
    settings: Settings,
    *,
    large_table_mb: float | None = None,
) -> TableSizeReport | None:
    threshold_mb = large_table_mb
    if threshold_mb is None:
        threshold_mb = float(
            getattr(settings, "large_table_mb", DEFAULT_LARGE_TABLE_MB) or DEFAULT_LARGE_TABLE_MB
        )
    threshold = int(threshold_mb * 1024 * 1024)

    db = creds.database.replace("'", "''")
    sql = (
        "SELECT TABLE_NAME, ENGINE, TABLE_ROWS, "
        "IFNULL(DATA_LENGTH,0), IFNULL(INDEX_LENGTH,0) "
        f"FROM information_schema.TABLES WHERE TABLE_SCHEMA='{db}' "
        "AND TABLE_TYPE='BASE TABLE' "
        "ORDER BY (IFNULL(DATA_LENGTH,0)+IFNULL(INDEX_LENGTH,0)) DESC"
    )

    raw = _query_via_cli(creds, sql)
    if raw is None:
        raw = _query_via_pymysql(creds, sql)
    if raw is None:
        return None

    tables: list[TableSizeRow] = []
    for r in raw:
        data_b = int(float(r["data"]))
        index_b = int(float(r["index"]))
        total = data_b + index_b
        tables.append(
            TableSizeRow(
                table=r["table"],
                engine=r["engine"],
                rows_approx=int(float(r["rows"])),
                data_bytes=data_b,
                index_bytes=index_b,
                total_bytes=total,
                large=total >= threshold,
            )
        )
    large = [t for t in tables if t.large]
    report = TableSizeReport(
        database=creds.database,
        host=f"{creds.host}:{creds.port}",
        generated_at=datetime.now(timezone.utc).isoformat(),
        large_threshold_bytes=threshold,
        tables=tables,
        large_tables=large,
    )

    out = settings.local_backup_root / ".mysql_table_sizes.json"
    payload = {
        "database": report.database,
        "host": report.host,
        "generated_at": report.generated_at,
        "large_threshold_bytes": report.large_threshold_bytes,
        "large_threshold_mb": threshold_mb,
        "table_count": len(tables),
        "large_count": len(large),
        "tables": [asdict(t) for t in tables[:200]],
        "large_tables": [asdict(t) for t in large],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if large:
        log.warning(
            "Large tables (≥%s MB) in %s: %s",
            threshold_mb,
            creds.database,
            ", ".join(f"{t.table}={t.total_bytes/1024/1024:.1f}MB" for t in large[:20]),
        )
    else:
        log.info(
            "No tables ≥%s MB in %s (%s tables)", threshold_mb, creds.database, len(tables)
        )
    return report
