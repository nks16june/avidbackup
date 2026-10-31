from __future__ import annotations

from typing import Any

from avidbackup.databases.base import DbDumper
from avidbackup.databases.mysql import MysqlDumper
from avidbackup.databases.postgres import PostgresDumper
from avidbackup.databases.sqlite import SqliteDumper

_REGISTRY: dict[str, type[DbDumper]] = {
    "mysql": MysqlDumper,
    "mariadb": MysqlDumper,
    "postgres": PostgresDumper,
    "postgresql": PostgresDumper,
    "sqlite": SqliteDumper,
    "sqlite3": SqliteDumper,
}


def get_dumper(db_config: Any, settings: Any, *, remote_client: Any | None = None) -> DbDumper:
    db_type = (getattr(db_config, "type", None) or "mysql").lower()
    cls = _REGISTRY.get(db_type)
    if cls is None:
        raise ValueError(
            f"Unsupported database type {db_type!r}. "
            f"Supported: {', '.join(sorted(set(_REGISTRY)))}. "
            "MongoDB and others are on the roadmap."
        )
    return cls(db_config, settings, remote_client=remote_client)


def dump_all_databases(settings: Any, *, remote_client: Any | None = None):
    """Dump every enabled database on settings.databases; discover MySQL if empty."""
    from avidbackup.databases.base import DumpResult
    from avidbackup.store import DatabaseConfig

    dest = settings.local_mysql_dir
    dest.mkdir(parents=True, exist_ok=True)
    dbs = [d for d in (settings.databases or []) if getattr(d, "enabled", True)]
    if not dbs:
        # Legacy: one discovered MySQL database
        dbs = [
            DatabaseConfig(
                id="discovered",
                label="discovered",
                type="mysql",
                discover_from_site=True,
                enabled=True,
            )
        ]
    total = DumpResult()
    for db in dbs:
        dumper = get_dumper(db, settings, remote_client=remote_client)
        try:
            one = dumper.dump(dest)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).error("Dump failed for %s: %s", getattr(db, "id", "?"), exc)
            total.failed += 1
            continue
        total.paths.extend(one.paths)
        total.bytes_written += one.bytes_written
        total.failed += one.failed
        if one.table_report:
            total.table_report = one.table_report
        if one.source:
            total.source = (total.source + "; " if total.source else "") + one.source
    return total
