from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from avidbackup.secure_cli import mysql_defaults_file, pgpass_file, resolve_secret
from avidbackup.store import DatabaseConfig
from avidbackup.util_bin import find_mysql, find_pg_restore, find_psql

log = logging.getLogger(__name__)


def newest_dump(mysql_dir: Path, database: str) -> Path | None:
    stem = (database or "").strip()
    if not stem:
        return None
    patterns = [
        f"{stem}_*.sql",
        f"{stem}_*.sql.gz",
        f"{stem}_*.dump",
        f"{stem}_*.dump.gz",
        f"{stem}_*.sqlite",
        f"{stem}_*.sqlite.gz",
    ]
    found: list[Path] = []
    for pat in patterns:
        found.extend(p for p in mysql_dir.glob(pat) if p.is_file())
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def build_restore_plan(
    db: DatabaseConfig,
    dump_path: Path,
    *,
    target_host: str | None = None,
    target_database: str | None = None,
    target_path: Path | None = None,
) -> dict:
    db_type = (db.type or "mysql").lower()
    host = target_host or db.host
    source_db = db.database or Path(db.path or dump_path.stem).stem
    dest_db = (target_database or "").strip() or source_db
    return {
        "type": db_type,
        "dump": dump_path,
        "host": host,
        "port": int(db.port or (5432 if db_type.startswith("post") else 3306)),
        "user": db.user,
        "source_database": source_db,
        "target_database": dest_db,
        "target_path": target_path,
        "ssl": bool(getattr(db, "ssl", False)),
    }


def describe_plan(plan: dict) -> str:
    t = plan["type"]
    dump = plan["dump"]
    if t in {"mysql", "mariadb"}:
        return (
            f'mysql --defaults-extra-file=<temp-option-file> {plan["target_database"]} '
            f'< {dump}   # password never on argv'
        )
    if t in {"postgres", "postgresql"}:
        if str(dump).endswith(".dump") or str(dump).endswith(".dump.gz"):
            return (
                f'pg_restore -h {plan["host"]} -p {plan["port"]} -U {plan["user"]} '
                f'-d {plan["target_database"]} {dump}   # via PGPASSFILE'
            )
        return (
            f'psql -h {plan["host"]} -p {plan["port"]} -U {plan["user"]} '
            f'-d {plan["target_database"]} < {dump}   # via PGPASSFILE'
        )
    if t.startswith("sqlite"):
        dest = plan["target_path"] or Path("<--target-path required>")
        return f'copy {dump} -> {dest}'
    return f"unsupported type {t}"


def execute_restore(
    db: DatabaseConfig,
    plan: dict,
    *,
    force: bool = False,
) -> int:
    """
    Execute restore. Refuses when target database name equals source unless force=True.
    Never puts the password on argv.
    """
    source = (plan["source_database"] or "").strip()
    target = (plan["target_database"] or "").strip()
    db_type = plan["type"]
    dump: Path = plan["dump"]

    if db_type.startswith("sqlite"):
        dest = plan.get("target_path")
        if dest is None:
            print("SQLite restore requires --target-path", file=sys.stderr)
            return 2
        dest = Path(dest)
        if db.path and Path(db.path).expanduser().resolve() == dest.resolve() and not force:
            print(
                f"Refusing to overwrite source SQLite path {dest} without --force",
                file=sys.stderr,
            )
            return 2
        return _restore_sqlite(dump, dest)

    if source and target and source == target and not force:
        print(
            f"Refusing to restore into source database {target!r} without --force. "
            "Pass --target-database <staging_name> or --force after review.",
            file=sys.stderr,
        )
        return 2

    password = resolve_secret(db.password, getattr(db, "password_env", "") or "")
    if db_type in {"mysql", "mariadb"}:
        return _restore_mysql(plan, password=password, dump=dump)
    if db_type in {"postgres", "postgresql"}:
        return _restore_postgres(plan, password=password, dump=dump)
    print(f"Unsupported database type {db_type}", file=sys.stderr)
    return 2


def _open_dump_bytes(dump: Path):
    if str(dump).endswith(".gz"):
        return gzip.open(dump, "rb")
    return dump.open("rb")


def _assert_no_password_argv(cmd: list[str], password: str) -> None:
    if not password:
        return
    joined = " ".join(cmd)
    if password in joined or any(a.startswith("--password=") for a in cmd):
        raise RuntimeError("Refusing to run restore: password would appear on argv")


def _restore_mysql(plan: dict, *, password: str, dump: Path) -> int:
    exe = find_mysql()
    if not exe:
        print("mysql client not found on PATH", file=sys.stderr)
        return 2
    with mysql_defaults_file(
        host=plan["host"],
        port=plan["port"],
        user=plan["user"],
        password=password,
    ) as defaults:
        cmd = [
            exe,
            f"--defaults-extra-file={defaults}",
            plan["target_database"],
        ]
        _assert_no_password_argv(cmd, password)
        log.info("Restoring MySQL into %s (dump=%s)", plan["target_database"], dump.name)
        with _open_dump_bytes(dump) as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:500]
        print(f"mysql restore failed: {err}", file=sys.stderr)
        return 1
    print(f"Restored MySQL database {plan['target_database']!r} from {dump}")
    return 0


def _restore_postgres(plan: dict, *, password: str, dump: Path) -> int:
    is_custom = str(dump).endswith(".dump") or str(dump).endswith(".dump.gz")
    if is_custom:
        pg_restore = find_pg_restore()
        if not pg_restore:
            print(
                "pg_restore not found. Run: avidbackup tools install-postgres",
                file=sys.stderr,
            )
            return 2
        work = dump
        tmp: Path | None = None
        if str(dump).endswith(".gz"):
            tmp = dump.parent / (dump.name[:-3] + ".tmp_restore")
            with gzip.open(dump, "rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            work = tmp
        try:
            with pgpass_file(
                host=plan["host"],
                port=plan["port"],
                database=plan["target_database"],
                user=plan["user"],
                password=password,
            ) as pgpass:
                env = os.environ.copy()
                env["PGPASSFILE"] = str(pgpass)
                env.pop("PGPASSWORD", None)
                cmd = [
                    pg_restore,
                    "-h",
                    plan["host"],
                    "-p",
                    str(plan["port"]),
                    "-U",
                    plan["user"],
                    "-d",
                    plan["target_database"],
                    "--no-owner",
                    "--no-acl",
                    str(work),
                ]
                _assert_no_password_argv(cmd, password)
                proc = subprocess.run(cmd, capture_output=True, env=env, check=False)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        if proc.returncode not in {0, 1}:
            print(proc.stderr.decode("utf-8", errors="replace")[:500], file=sys.stderr)
            return 1
        print(f"Restored PostgreSQL database {plan['target_database']!r} from {dump}")
        return 0

    psql = find_psql()
    if not psql:
        print("psql not found on PATH", file=sys.stderr)
        return 2
    with pgpass_file(
        host=plan["host"],
        port=plan["port"],
        database=plan["target_database"],
        user=plan["user"],
        password=password,
    ) as pgpass:
        env = os.environ.copy()
        env["PGPASSFILE"] = str(pgpass)
        env.pop("PGPASSWORD", None)
        cmd = [
            psql,
            "-h",
            plan["host"],
            "-p",
            str(plan["port"]),
            "-U",
            plan["user"],
            "-d",
            plan["target_database"],
            "-v",
            "ON_ERROR_STOP=1",
        ]
        _assert_no_password_argv(cmd, password)
        with _open_dump_bytes(dump) as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", errors="replace")[:500], file=sys.stderr)
        return 1
    print(f"Restored PostgreSQL database {plan['target_database']!r} from {dump}")
    return 0


def _restore_sqlite(dump: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if str(dump).endswith(".gz"):
        with gzip.open(dump, "rb") as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copy2(dump, dest)
    print(f"Restored SQLite file to {dest}")
    return 0
