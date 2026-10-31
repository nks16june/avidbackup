from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from avidbackup.databases.base import DbDumper, DumpResult
from avidbackup.databases.compress import maybe_gzip

log = logging.getLogger(__name__)


class SqliteDumper(DbDumper):
    name = "sqlite"
    type = "sqlite"

    def _local_source(self) -> Path:
        path = Path(getattr(self.db, "path", "") or "").expanduser()
        if path.is_file():
            return path
        # Optional: fetch remote SQLite via FTP/SFTP into a temp location under dest later
        if self.remote is not None and getattr(self.db, "path", ""):
            remote = self.db.path
            tmp = self.settings.local_mysql_dir / f".sqlite_fetch_{self.db.id}"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            self.remote.download_file(remote, tmp)
            return tmp
        raise FileNotFoundError(f"SQLite path not found: {self.db.path!r}")

    def test_connection(self) -> None:
        src = self._local_source()
        conn = sqlite3.connect(str(src))
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()

    def dump(self, dest_dir: Path) -> DumpResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = self._local_source()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = Path(self.db.database or src.stem).name
        dest = dest_dir / f"{name}_{stamp}.sqlite"
        log.info("SQLite online backup %s → %s", src, dest)
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        # completeness marker sidecar
        dest.with_suffix(dest.suffix + ".done").write_text("Dump completed\n", encoding="utf-8")
        path = maybe_gzip(
            dest,
            getattr(self.db, "compress", None) or getattr(self.settings, "compress_dumps", "gzip"),
        )
        return DumpResult(
            paths=[path],
            bytes_written=path.stat().st_size,
            source=str(src),
        )
