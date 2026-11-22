"""End-to-end: real PHP helper + real (temporary) MySQL, and the SSH tunnel + MySQL dumper.

Skipped unless php (with mysqli) and mysqld are available; see tests/helper_env.py.
"""

from __future__ import annotations

import gzip
import subprocess
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest
from helper_env import (  # noqa: F401
    connect,
    mysql_server,
    php_exe,
    php_has_mysqli,
    php_site,
    seed_database,
    table_checksums,
)
from ssh_test_server import PASSWORD, USER, SshTestServer

from avidbackup.databases.mysql import MysqlDumper
from avidbackup.databases.php_helper import PhpHelperDumper
from avidbackup.databases.registry import get_dumper
from avidbackup.php_helper import HelperClient, generate_helper, new_token
from avidbackup.store import DatabaseConfig

NAME = "avidbackup-it.php"


@pytest.fixture
def source_db(mysql_server):  # noqa: F811
    return seed_database(mysql_server, "avb_src")


@pytest.fixture
def helper(mysql_server, source_db, php_site, php_has_mysqli):  # noqa: F811
    if not php_has_mysqli:
        pytest.skip("php has no mysqli")
    token = new_token()
    src = generate_helper(token=token, db_host=mysql_server.host, db_port=mysql_server.port,
                          db_user=mysql_server.user, db_password=mysql_server.password, db_name=source_db)
    site = php_site({NAME: src})
    return SimpleNamespace(url=site.url(NAME), token=token, client=HelperClient(site.url(NAME), token),
                           db=source_db)


def _restore(server, sql: Path, target: str) -> None:  # noqa: ANN001
    if not server.client:
        pytest.skip("mysql client not available to restore the dump")
    conn = connect(server)
    conn.cursor().execute(f"DROP DATABASE IF EXISTS `{target}`")
    conn.cursor().execute(f"CREATE DATABASE `{target}` CHARACTER SET utf8mb4")
    conn.close()
    with sql.open("rb") as fh:
        run = subprocess.run(
            [server.client, "--no-defaults", f"-h{server.host}", f"-P{server.port}", f"-u{server.user}",
             "--default-character-set=utf8mb4", target],
            stdin=fh, capture_output=True, timeout=300,
        )
    assert run.returncode == 0, run.stderr.decode(errors="replace")


# ---- the helper ----------------------------------------------------------------------------


def test_helper_reports_the_database(helper):
    assert helper.client.ping()["mysqli"] is True
    info = helper.client.check()
    assert info["database"] == helper.db and info["tables"] >= 5


def test_helper_lists_table_sizes_largest_first(helper):
    tables = helper.client.tables()
    names = [t["table"] for t in tables]
    assert {"users", "blobs", "big", "audit", "odd`name"} <= set(names)
    assert "adults" not in names  # views are not tables
    totals = [t["data_bytes"] + t["index_bytes"] for t in tables]
    assert totals == sorted(totals, reverse=True)


def test_dump_restores_to_an_identical_database(helper, mysql_server, tmp_path):  # noqa: F811
    dump = tmp_path / "dump.sql"
    helper.client.dump_to(dump)
    text = dump.read_text(encoding="utf-8")
    assert "-- Dump completed" in text and "CREATE TABLE `users`" in text
    assert "INSERT INTO `big` VALUES" in text and "0x" in text  # binary written as hex

    _restore(mysql_server, dump, "avb_restored")
    for table in ("users",):
        a = connect(mysql_server, helper.db).cursor()
        b = connect(mysql_server, "avb_restored").cursor()
        a.execute(f"SELECT * FROM {table} ORDER BY id")
        b.execute(f"SELECT * FROM {table} ORDER BY id")
        assert a.fetchall() == b.fetchall()
    assert table_checksums(mysql_server, "avb_restored") == table_checksums(mysql_server, helper.db)

    conn = connect(mysql_server, "avb_restored")
    cur = conn.cursor()
    cur.execute("SELECT name, note FROM users ORDER BY id")
    rows = cur.fetchall()
    assert rows[0] == ("O'Brien", 'He said "hi" \\ backslash')
    assert rows[1][0] == "Zoë 日本語 🙂" and rows[1][1] == "line1\nline2\ttab"
    cur.execute("SELECT data, fixed, blank FROM blobs WHERE id = 1")
    data, fixed, blank = cur.fetchone()
    assert data == bytes(range(256)) + b"\x00\x00\xff'\"\\" and fixed == b"\x00\x01\x02\x03" and blank == b""
    cur.execute("SELECT COUNT(*) FROM big")
    assert cur.fetchone()[0] == 2500
    cur.execute("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = 'avb_restored'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = 'avb_restored'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = 'avb_restored'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT COUNT(*) FROM audit")  # the one row from the source; the restore must not re-fire it
    assert cur.fetchone()[0] == 1
    conn.close()


def test_dump_is_a_consistent_read_only_snapshot(helper, mysql_server, tmp_path):  # noqa: F811
    before = table_checksums(mysql_server, helper.db)
    helper.client.dump_to(tmp_path / "d.sql")
    assert table_checksums(mysql_server, helper.db) == before  # the helper changed nothing


# ---- the dumper --------------------------------------------------------------------------------


def _settings(tmp: Path | None = None):  # noqa: ANN202
    return SimpleNamespace(local_backup_root=tmp or Path("."), compress_dumps="gzip", large_table_mb=50, mysql_dump_timeout=120, mysql_ssl=False,
                           protocol="ftp", host="x", port=21, user="u", password="p", mysql_host="",
                           mysql_port=0, remote_files_root="/", mysql_config_candidates=[], mysql_profile="web",
                           local_files_dir=None)


def test_php_helper_dumper_writes_a_compressed_checked_dump(helper, tmp_path):
    db = DatabaseConfig(id="main", via="php", database=helper.db, helper_url=helper.url,
                        helper_token=helper.token)
    dumper = get_dumper(db, _settings(tmp_path))
    assert isinstance(dumper, PhpHelperDumper)
    dumper.test_connection()
    result = dumper.dump(tmp_path)
    assert result.failed == 0 and len(result.paths) == 1
    path = result.paths[0]
    assert path.name.endswith(".sql.gz") and Path(str(path) + ".sha256").is_file()
    assert b"INSERT INTO `users`" in gzip.decompress(path.read_bytes())
    names = {r.table for r in result.table_report.tables}
    assert {"users", "big"} <= names


def test_php_helper_dumper_reports_a_wrong_token_as_a_failed_dump(helper, tmp_path):
    db = DatabaseConfig(id="main", via="php", database=helper.db, helper_url=helper.url,
                        helper_token="wrong-" + "q" * 40)
    result = get_dumper(db, _settings(tmp_path)).dump(tmp_path)
    assert result.failed == 1 and not result.paths
    assert not list(tmp_path.glob("*.sql*"))


def test_php_helper_is_only_offered_for_mysql(tmp_path):
    db = DatabaseConfig(id="x", type="postgres", via="php", helper_url="https://e.example/a.php",
                        helper_token="t" * 30)
    with pytest.raises(ValueError, match="MySQL"):
        get_dumper(db, _settings(tmp_path))


# ---- SSH tunnel + the normal MySQL dumper -------------------------------------------------------


def test_mysql_dump_through_an_ssh_tunnel(mysql_server, source_db, tmp_path):  # noqa: F811
    ssh = SshTestServer(paramiko.RSAKey.generate(2048))
    try:
        db = DatabaseConfig(
            id="main", type="mysql", via="ssh", host="127.0.0.1", port=mysql_server.port,
            user=mysql_server.user, password=mysql_server.password, database=source_db,
            ssh_host="127.0.0.1", ssh_port=ssh.port, ssh_user=USER, ssh_password=PASSWORD,
            ssh_host_key_policy="insecure",
        )
        dumper = MysqlDumper(db, _settings(tmp_path))
        dumper.test_connection()
        result = dumper.dump(tmp_path)
        assert result.failed == 0 and result.paths
        text = gzip.decompress(result.paths[0].read_bytes()).decode("utf-8", "replace")
        assert "users" in text and "O'Brien" in text.replace("O\\'Brien", "O'Brien")
        assert f"127.0.0.1:{mysql_server.port}" in ssh.logins  # the traffic really went through SSH
    finally:
        ssh.close()
