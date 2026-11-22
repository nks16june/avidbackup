from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from avidbackup.databases.base import DbDumper, DumpResult, with_tunnel
from avidbackup.databases.compress import maybe_gzip
from avidbackup.secure_cli import pgpass_file, resolve_secret
from avidbackup.util_bin import find_pg_dump, find_psql

log = logging.getLogger(__name__)


class PostgresDumper(DbDumper):
    name = "postgres"
    type = "postgres"

    def _password(self) -> str:
        return resolve_secret(self.db.password, getattr(self.db, "password_env", "") or "")

    @with_tunnel
    def test_connection(self) -> None:
        psql = find_psql()
        if not psql:
            raise RuntimeError("psql not found. Install PostgreSQL client tools.")
        env = os.environ.copy()
        env["PGPASSWORD"] = self._password()
        if getattr(self.db, "ssl", False):
            env["PGSSLMODE"] = "require"
        cmd = [
            psql,
            "-h",
            self.db.host,
            "-p",
            str(int(self.db.port or 5432)),
            "-U",
            self.db.user,
            "-d",
            self.db.database,
            "-c",
            "SELECT 1",
        ]
        proc = subprocess.run(cmd, capture_output=True, env=env, timeout=30, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:400])

    @with_tunnel
    def dump(self, dest_dir: Path) -> DumpResult:
        exe = find_pg_dump()
        if not exe:
            raise RuntimeError(
                "pg_dump not found. Install PostgreSQL client tools "
                "(apt install postgresql-client / brew install libpq)."
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fmt = (getattr(self.db, "format", None) or "plain").lower()
        if fmt == "custom":
            local_path = dest_dir / f"{self.db.database}_{stamp}.dump"
            fmt_args = ["-Fc"]
        else:
            local_path = dest_dir / f"{self.db.database}_{stamp}.sql"
            fmt_args = ["-Fp"]

        password = self._password()
        env = os.environ.copy()
        # Prefer PGPASSFILE over argv; also set PGPASSWORD in env only (never argv)
        with pgpass_file(
            host=self.db.host,
            port=int(self.db.port or 5432),
            database=self.db.database or "*",
            user=self.db.user,
            password=password,
        ) as pgpass:
            env["PGPASSFILE"] = str(pgpass)
            env.pop("PGPASSWORD", None)
            if getattr(self.db, "ssl", False):
                env["PGSSLMODE"] = "require"
            cmd = [
                exe,
                "-h",
                self.db.host,
                "-p",
                str(int(self.db.port or 5432)),
                "-U",
                self.db.user,
                "-d",
                self.db.database,
                *fmt_args,
                "-f",
                str(local_path),
            ]
            log.info("pg_dump → %s:%s db=%s", self.db.host, self.db.port, self.db.database)
            proc = subprocess.run(cmd, capture_output=True, env=env, timeout=self.settings.mysql_dump_timeout, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")[:500]
            log.error("pg_dump failed: %s", err)
            local_path.unlink(missing_ok=True)
            return DumpResult(failed=1, source=f"{self.db.host}/{self.db.database}")
        if fmt != "custom":
            with local_path.open("a", encoding="utf-8") as fh:
                fh.write("\n-- Dump completed\n")
        path = maybe_gzip(
            local_path,
            getattr(self.db, "compress", None) or getattr(self.settings, "compress_dumps", "gzip"),
        )
        return DumpResult(
            paths=[path],
            bytes_written=path.stat().st_size,
            source=f"{self.db.host}:{self.db.port}/{self.db.database}",
            table_report=self.table_sizes(),
        )

    @with_tunnel
    def table_sizes(self):
        psql = find_psql()
        if not psql:
            return None
        env = os.environ.copy()
        env["PGPASSWORD"] = self._password()
        sql = (
            "SELECT relname, pg_total_relation_size(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind='r' AND n.nspname='public' "
            "ORDER BY 2 DESC LIMIT 50;"
        )
        cmd = [
            psql,
            "-h",
            self.db.host,
            "-p",
            str(int(self.db.port or 5432)),
            "-U",
            self.db.user,
            "-d",
            self.db.database,
            "-t",
            "-A",
            "-F",
            ",",
            "-c",
            sql,
        ]
        proc = subprocess.run(cmd, capture_output=True, env=env, timeout=60, check=False)
        if proc.returncode != 0:
            return None
        # Lightweight ad-hoc report dict for UI/logs
        rows = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            parts = line.split(",", 1)
            if len(parts) == 2:
                rows.append({"table": parts[0], "total_bytes": int(parts[1] or 0)})
        return {"database": self.db.database, "tables": rows}
