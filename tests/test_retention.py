from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from avidbackup.retention import prune_mysql_dumps


def _touch(path: Path, days_ago: int) -> None:
    path.write_text("-- dump\n", encoding="utf-8")
    past = (datetime.now() - timedelta(days=days_ago)).timestamp()
    import os

    os.utime(path, (past, past))


def test_prune_old_only(tmp_path: Path):
    mysql = tmp_path / "mysql"
    files = tmp_path / "files"
    mysql.mkdir()
    files.mkdir()
    site = files / "index.html"
    site.write_text("ok", encoding="utf-8")
    old = mysql / "old.sql"
    new = mysql / "new.sql"
    gz = mysql / "old2.sql.gz"
    dump = mysql / "old3.dump"
    _touch(old, 20)
    _touch(new, 1)
    _touch(gz, 20)
    _touch(dump, 20)
    removed = prune_mysql_dumps(mysql, keep_days=15)
    assert removed >= 3
    assert new.is_file()
    assert site.is_file()
    assert not old.is_file()


def test_boundary_not_pruned(tmp_path: Path):
    mysql = tmp_path / "mysql"
    mysql.mkdir()
    recent = mysql / "recent.sql"
    _touch(recent, 14)
    assert prune_mysql_dumps(mysql, keep_days=15) == 0
    assert recent.is_file()
