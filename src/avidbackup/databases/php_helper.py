from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from avidbackup.databases.base import DbDumper, DumpResult
from avidbackup.databases.compress import maybe_gzip
from avidbackup.php_helper import HelperClient, HelperError
from avidbackup.secure_cli import resolve_secret

log = logging.getLogger(__name__)


class PhpHelperDumper(DbDumper):
    """MySQL/MariaDB through the uploaded PHP helper (for hosts without remote MySQL or SSH)."""

    name = "mysql-php-helper"
    type = "mysql"

    def _client(self) -> HelperClient:
        db = self.db
        if not db.helper_url:
            raise HelperError("No helper URL is set. Run: avidbackup helper install")
        token = resolve_secret(db.helper_token, getattr(db, "helper_token_env", "") or "")
        return HelperClient(db.helper_url, token)

    def test_connection(self) -> None:
        info = self._client().check()
        log.info("PHP helper OK: %s tables in %s", info.get("tables"), info.get("database"))

    def dump(self, dest_dir: Path) -> DumpResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = self.db.database or self.db.id or "database"
        local = dest_dir / f"{name}_{stamp}.sql"
        try:
            self._client().dump_to(local)
        except HelperError as exc:
            log.error("PHP helper dump failed for %s: %s", self.db.id, exc)
            return DumpResult(failed=1, source=f"php-helper/{name}: {exc}")
        path = maybe_gzip(
            local,
            getattr(self.db, "compress", None) or getattr(self.settings, "compress_dumps", "gzip"),
        )
        return DumpResult(
            paths=[path],
            bytes_written=path.stat().st_size,
            source=f"php-helper/{name}",
            table_report=self.table_sizes(),
        )

    def table_sizes(self):  # noqa: ANN201
        from avidbackup.mysql_tables import DEFAULT_LARGE_TABLE_MB, TableSizeReport, TableSizeRow

        try:
            tables = self._client().tables()
        except HelperError as exc:
            log.warning("Could not read table sizes through the helper: %s", exc)
            return None
        threshold_mb = float(getattr(self.settings, "large_table_mb", DEFAULT_LARGE_TABLE_MB) or 50)
        limit = int(threshold_mb * 1024 * 1024)
        rows = [
            TableSizeRow(
                table=str(t.get("table", "")),
                engine=str(t.get("engine", "")),
                rows_approx=int(t.get("rows", 0)),
                data_bytes=int(t.get("data_bytes", 0)),
                index_bytes=int(t.get("index_bytes", 0)),
                total_bytes=int(t.get("data_bytes", 0)) + int(t.get("index_bytes", 0)),
                large=(int(t.get("data_bytes", 0)) + int(t.get("index_bytes", 0))) >= limit,
            )
            for t in tables
        ]
        return TableSizeReport(
            database=self.db.database,
            host="php-helper",
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            large_threshold_bytes=limit,
            tables=rows,
            large_tables=[r for r in rows if r.large],
        )
