from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from avidbackup.databases.sqlite import SqliteDumper
from avidbackup.store import DatabaseConfig


def test_sqlite_online_backup_round_trip(tmp_path: Path):
    src = tmp_path / "app.sqlite"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t (name) VALUES ('alpha'), ('beta')")
    conn.commit()
    conn.close()

    dest_dir = tmp_path / "dumps"
    settings = SimpleNamespace(
        local_mysql_dir=dest_dir,
        compress_dumps="none",
    )
    db = DatabaseConfig(
        id="local",
        type="sqlite",
        path=str(src),
        database="app",
        compress="none",
    )
    result = SqliteDumper(db, settings).dump(dest_dir)
    assert result.failed == 0
    assert result.paths
    dump = result.paths[0]
    assert dump.is_file()

    # Round-trip: open dump and read rows
    check = sqlite3.connect(str(dump))
    rows = check.execute("SELECT name FROM t ORDER BY id").fetchall()
    check.close()
    assert rows == [("alpha",), ("beta",)]


def test_sqlite_gzip_round_trip(tmp_path: Path):
    src = tmp_path / "app.sqlite"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t DEFAULT VALUES")
    conn.commit()
    conn.close()

    dest_dir = tmp_path / "dumps"
    settings = SimpleNamespace(local_mysql_dir=dest_dir, compress_dumps="gzip")
    db = DatabaseConfig(id="local", type="sqlite", path=str(src), database="app", compress="gzip")
    result = SqliteDumper(db, settings).dump(dest_dir)
    dump = result.paths[0]
    assert dump.name.endswith(".gz")
    raw = tmp_path / "restored.sqlite"
    with gzip.open(dump, "rb") as fh:
        raw.write_bytes(fh.read())
    check = sqlite3.connect(str(raw))
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    check.close()
