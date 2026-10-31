from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from avidbackup.databases.mysql import MysqlDumper
from avidbackup.databases.postgres import PostgresDumper
from avidbackup.store import DatabaseConfig


def test_mysql_dump_argv_has_no_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        if "stdout" in kwargs and kwargs["stdout"] is not None:
            kwargs["stdout"].write(b"-- dump\n")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("avidbackup.databases.mysql.find_mysqldump", lambda: "mysqldump")
    monkeypatch.setattr("avidbackup.databases.mysql.subprocess.run", fake_run)
    monkeypatch.setattr("avidbackup.databases.mysql.collect_table_sizes", lambda *a, **k: None)
    monkeypatch.setattr("avidbackup.databases.mysql.maybe_gzip", lambda path, _c: path)

    settings = SimpleNamespace(
        mysql_dump_timeout=30,
        compress_dumps="none",
        large_table_mb=50,
        host="ftp.example.com",
        mysql_host="",
        mysql_port=3306,
        mysql_profile="web",
        mysql_config_candidates=[],
        mysql_ssl=False,
        remote_files_root="/",
    )
    db = DatabaseConfig(
        id="main",
        type="mysql",
        host="db.example.com",
        port=3306,
        user="u",
        password="SUPERSECRET",
        database="app",
        compress="none",
    )
    result = MysqlDumper(db, settings).dump(tmp_path)
    assert result.failed == 0
    cmd = captured["cmd"]
    assert any(a.startswith("--defaults-extra-file=") for a in cmd)
    assert not any(a.startswith("--password=") for a in cmd)
    assert "SUPERSECRET" not in " ".join(cmd)


def test_postgres_dump_argv_has_no_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env") or {}
        out = Path(cmd[cmd.index("-f") + 1])
        out.write_text("-- dump\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr=b"", stdout=b"")

    monkeypatch.setattr("avidbackup.databases.postgres.find_pg_dump", lambda: "pg_dump")
    monkeypatch.setattr("avidbackup.databases.postgres.find_psql", lambda: None)
    monkeypatch.setattr("avidbackup.databases.postgres.subprocess.run", fake_run)
    monkeypatch.setattr("avidbackup.databases.postgres.maybe_gzip", lambda path, _c: path)

    settings = SimpleNamespace(mysql_dump_timeout=30, compress_dumps="none")
    db = DatabaseConfig(
        id="pg",
        type="postgres",
        host="db.example.com",
        port=5432,
        user="u",
        password="PGSECRET99",
        database="analytics",
        format="plain",
        compress="none",
    )
    result = PostgresDumper(db, settings).dump(tmp_path)
    assert result.failed == 0
    cmd = captured["cmd"]
    assert "PGSECRET99" not in " ".join(cmd)
    assert not any("password" in a.lower() for a in cmd)
    assert "PGPASSFILE" in captured["env"]
    assert "PGPASSWORD" not in captured["env"]
