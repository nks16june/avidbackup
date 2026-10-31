from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from avidbackup.databases.base import DbDumper, DumpResult
from avidbackup.databases.compress import maybe_gzip
from avidbackup.mysql_config import (
    MysqlCredentials,
    discover_mysql_credentials,
    resolve_direct_host,
)
from avidbackup.mysql_tables import collect_table_sizes
from avidbackup.secure_cli import mysql_defaults_file, resolve_secret
from avidbackup.util_bin import find_mysqldump

log = logging.getLogger(__name__)


class MysqlDumper(DbDumper):
    name = "mysql"
    type = "mysql"

    def _creds(self) -> MysqlCredentials:
        db = self.db
        if db.discover_from_site and self.remote is not None and not (db.host and db.user and db.database):
            discovered = discover_mysql_credentials(
                self.remote,
                self.settings.remote_files_root,
                self.settings.mysql_config_candidates,
                profile=self.settings.mysql_profile,
            )
            host = resolve_direct_host(
                discovered,
                mysql_host_override=self.settings.mysql_host,
                ftp_host=self.settings.host,
            )
            return MysqlCredentials(
                host=host,
                port=self.settings.mysql_port or discovered.port,
                user=discovered.user,
                password=discovered.password,
                database=discovered.database,
                source_path=discovered.source_path,
                profile=discovered.profile,
            )
        password = resolve_secret(db.password, getattr(db, "password_env", "") or "")
        return MysqlCredentials(
            host=db.host,
            port=int(db.port or 3306),
            user=db.user,
            password=password,
            database=db.database,
            source_path=f"ui:{db.id}",
            profile="explicit",
        )

    def test_connection(self) -> None:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("pymysql required") from exc
        c = self._creds()
        kwargs: dict = dict(
            host=c.host,
            port=c.port,
            user=c.user,
            password=c.password,
            database=c.database,
            connect_timeout=15,
        )
        if getattr(self.db, "ssl", False) or getattr(self.settings, "mysql_ssl", False):
            kwargs["ssl"] = {"check_hostname": True}
        conn = pymysql.connect(**kwargs)
        conn.close()

    def dump(self, dest_dir: Path) -> DumpResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        creds = self._creds()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = dest_dir / f"{creds.database}_{stamp}.sql"
        result = self._mysqldump(creds, local_path)
        if result is None:
            result = self._pymysql(creds, local_path)
        if result is None:
            return DumpResult(failed=1, source=f"{creds.host}/{creds.database}")
        path, size = result
        path = maybe_gzip(path, getattr(self.db, "compress", None) or getattr(self.settings, "compress_dumps", "gzip"))
        report = collect_table_sizes(creds, self.settings)
        return DumpResult(
            paths=[path],
            bytes_written=path.stat().st_size if path.is_file() else size,
            source=f"{creds.host}:{creds.port}/{creds.database}",
            table_report=report,
        )

    def table_sizes(self):
        return collect_table_sizes(self._creds(), self.settings)

    def _mysqldump(self, creds: MysqlCredentials, local_path: Path) -> tuple[Path, int] | None:
        exe = find_mysqldump()
        if not exe:
            log.info("mysqldump not found; using PyMySQL")
            return None
        try:
            with mysql_defaults_file(
                host=creds.host, port=creds.port, user=creds.user, password=creds.password
            ) as defaults:
                cmd = [
                    exe,
                    f"--defaults-extra-file={defaults}",
                    "--single-transaction",
                    "--quick",
                    "--routines",
                    "--triggers",
                    "--events",
                    "--hex-blob",
                    "--default-character-set=utf8mb4",
                    "--set-gtid-purged=OFF",
                    creds.database,
                ]
                log.info("mysqldump → %s:%s db=%s", creds.host, creds.port, creds.database)
                with local_path.open("wb") as fh:
                    proc = subprocess.run(
                        cmd,
                        stdout=fh,
                        stderr=subprocess.PIPE,
                        timeout=self.settings.mysql_dump_timeout,
                        check=False,
                    )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")[:500]
                log.warning("mysqldump failed (%s): %s", proc.returncode, err)
                local_path.unlink(missing_ok=True)
                return None
            # append completeness marker for verify
            with local_path.open("ab") as fh:
                fh.write(b"\n-- Dump completed\n")
            size = local_path.stat().st_size
            if size <= 0:
                local_path.unlink(missing_ok=True)
                return None
            return local_path, size
        except Exception as exc:  # noqa: BLE001
            log.warning("mysqldump error: %s", exc)
            local_path.unlink(missing_ok=True)
            return None

    def _pymysql(self, creds: MysqlCredentials, local_path: Path) -> tuple[Path, int] | None:
        try:
            import pymysql
            from pymysql.constants import CLIENT
        except ImportError:
            log.error("pymysql is required for fallback dump")
            return None
        kwargs: dict = dict(
            host=creds.host,
            port=creds.port,
            user=creds.user,
            password=creds.password,
            database=creds.database,
            charset="utf8mb4",
            connect_timeout=30,
            read_timeout=self.settings.mysql_dump_timeout,
            write_timeout=60,
            client_flag=CLIENT.MULTI_STATEMENTS,
        )
        if getattr(self.db, "ssl", False) or getattr(self.settings, "mysql_ssl", False):
            kwargs["ssl"] = {"check_hostname": True}
        try:
            conn = pymysql.connect(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error("MySQL connect failed: %s", exc)
            return None
        try:
            with local_path.open("w", encoding="utf-8", newline="\n") as fh, conn.cursor() as cur:
                fh.write("-- AvidBackup MySQL dump\n")
                fh.write(f"-- host={creds.host} db={creds.database}\n")
                fh.write(f"-- Generated: {datetime.now().isoformat()}\n\n")
                fh.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n")
                cur.execute("SHOW FULL TABLES")
                tables = [(row[0], row[1] if len(row) > 1 else "BASE TABLE") for row in cur.fetchall()]
                for table, kind in tables:
                    safe = table.replace("`", "``")
                    if kind.upper() == "VIEW":
                        cur.execute(f"SHOW CREATE VIEW `{safe}`")
                        row = cur.fetchone()
                        if row:
                            fh.write(f"DROP VIEW IF EXISTS `{safe}`;\n{row[1]};\n\n")
                        continue
                    cur.execute(f"SHOW CREATE TABLE `{safe}`")
                    row = cur.fetchone()
                    if not row:
                        continue
                    fh.write(f"DROP TABLE IF EXISTS `{safe}`;\n{row[1]};\n\n")
                    with conn.cursor(pymysql.cursors.SSCursor) as stream:
                        stream.execute(f"SELECT * FROM `{safe}`")
                        cols = [d[0] for d in (stream.description or [])]
                        if not cols:
                            continue
                        col_sql = ",".join(f"`{c.replace('`', '``')}`" for c in cols)
                        while True:
                            batch = stream.fetchmany(500)
                            if not batch:
                                break
                            for record in batch:
                                vals = []
                                for v in record:
                                    if v is None:
                                        vals.append("NULL")
                                    elif isinstance(v, (bytes, bytearray)):
                                        vals.append("0x" + v.hex())
                                    elif isinstance(v, (int, float)):
                                        vals.append(str(v))
                                    else:
                                        s = str(v).replace("\\", "\\\\").replace("'", "\\'")
                                        vals.append(f"'{s}'")
                                fh.write(
                                    f"INSERT INTO `{safe}` ({col_sql}) VALUES ({','.join(vals)});\n"
                                )
                    fh.write("\n")
                fh.write("SET FOREIGN_KEY_CHECKS=1;\n")
                fh.write("-- Dump completed\n")
        except Exception as exc:  # noqa: BLE001
            log.error("PyMySQL dump failed: %s", exc)
            local_path.unlink(missing_ok=True)
            return None
        finally:
            conn.close()
        return local_path, local_path.stat().st_size
