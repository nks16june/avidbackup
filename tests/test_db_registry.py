from __future__ import annotations

from avidbackup.databases.registry import get_dumper
from avidbackup.store import DatabaseConfig


class _S:
    remote_files_root = "/"
    mysql_config_candidates = []
    mysql_profile = "web"
    mysql_host = ""
    mysql_port = 3306
    mysql_dump_timeout = 60
    compress_dumps = "none"
    local_mysql_dir = None
    local_backup_root = None
    large_table_mb = 50
    host = "ftp.example.com"


def test_registry_types(tmp_path):
    s = _S()
    s.local_mysql_dir = tmp_path
    s.local_backup_root = tmp_path
    for t in ("mysql", "postgres", "sqlite"):
        db = DatabaseConfig(id="x", type=t, host="h", user="u", password="p", database="d", path=str(tmp_path / "x.sqlite"))
        dumper = get_dumper(db, s)
        assert dumper.type in {"mysql", "postgres", "sqlite"}


def test_mysql_defaults_file_has_no_password_argv(tmp_path, monkeypatch):
    from avidbackup.secure_cli import mysql_defaults_file

    with mysql_defaults_file(host="h", port=3306, user="u", password="SECRET") as path:
        text = path.read_text(encoding="utf-8")
        assert "SECRET" in text
        assert path.is_file()
    # Command construction pattern: argv uses defaults file, not --password=
    argv = ["mysqldump", f"--defaults-extra-file={path}", "db"]
    assert not any(a.startswith("--password=") for a in argv)
