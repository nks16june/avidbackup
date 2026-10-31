from __future__ import annotations

from pathlib import Path

from avidbackup.restore_db import build_restore_plan, execute_restore
from avidbackup.store import DatabaseConfig


def test_refuse_same_mysql_name_without_force(tmp_path: Path):
    dump = tmp_path / "app_20260101.sql"
    dump.write_text("-- Dump completed\n", encoding="utf-8")
    db = DatabaseConfig(
        id="main",
        type="mysql",
        host="h",
        user="u",
        password="p",
        database="app",
    )
    plan = build_restore_plan(db, dump, target_database="app")
    code = execute_restore(db, plan, force=False)
    assert code == 2


def test_sqlite_refuse_overwrite_source_without_force(tmp_path: Path):
    src = tmp_path / "live.sqlite"
    src.write_bytes(b"SQLite format 3\x00")
    dump = tmp_path / "live_backup.sqlite"
    dump.write_bytes(b"SQLite format 3\x00backup")
    db = DatabaseConfig(id="s", type="sqlite", path=str(src), database="live")
    plan = build_restore_plan(db, dump, target_path=src)
    assert execute_restore(db, plan, force=False) == 2
    # with force, overwrites
    assert execute_restore(db, plan, force=True) == 0
    assert src.read_bytes().startswith(b"SQLite format 3")
