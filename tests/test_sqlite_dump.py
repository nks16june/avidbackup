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


def test_remote_fetch_temp_file_is_removed(tmp_path):
    import sqlite3
    from types import SimpleNamespace

    from avidbackup.databases.sqlite import SqliteDumper

    remote_db = tmp_path / "remote.sqlite"
    c = sqlite3.connect(remote_db)
    c.execute("create table t(x)")
    c.commit()
    c.close()

    class FakeRemote:
        def download_file(self, remote, dest):
            dest.write_bytes(remote_db.read_bytes())

    mysql_dir = tmp_path / "mysql"
    settings = SimpleNamespace(local_mysql_dir=mysql_dir, compress_dumps="none")
    db = SimpleNamespace(id="d1", path="/remote.sqlite", database="site", compress="none")
    dumper = SqliteDumper(db, settings, remote_client=FakeRemote())
    dumper.dump(mysql_dir)
    assert not any(p.name.startswith(".sqlite_fetch_") for p in mysql_dir.iterdir())
