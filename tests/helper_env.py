"""Fixtures for end-to-end tests that need a real MySQL server and/or PHP.

Both are optional: tests skip when the tools are missing. Point the tests at them with
AVIDBACKUP_TEST_MYSQLD (path to mysqld) and AVIDBACKUP_TEST_PHP (path to php), or put them on PATH.
The MySQL server is a throwaway instance in a temp folder on a random port; it never touches
an installed MySQL.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, seconds: float = 30.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def find_php() -> str | None:
    return os.environ.get("AVIDBACKUP_TEST_PHP") or shutil.which("php")


def find_mysqld() -> str | None:
    return os.environ.get("AVIDBACKUP_TEST_MYSQLD") or shutil.which("mysqld")


@pytest.fixture(scope="session")
def php_exe() -> str:
    exe = find_php()
    if not exe:
        pytest.skip("php not available (set AVIDBACKUP_TEST_PHP)")
    check = subprocess.run([exe, "-m"], capture_output=True, text=True, timeout=30)
    if check.returncode != 0:
        pytest.skip("php does not run")
    return exe


@pytest.fixture(scope="session")
def php_has_mysqli(php_exe: str) -> bool:
    out = subprocess.run([php_exe, "-m"], capture_output=True, text=True, timeout=30).stdout.lower()
    return "mysqli" in out


@pytest.fixture(scope="session")
def mysql_server(tmp_path_factory: pytest.TempPathFactory):
    """A temporary, empty MySQL server (root, no password) on 127.0.0.1:<random port>."""
    pymysql = pytest.importorskip("pymysql")
    exe = find_mysqld()
    if not exe:
        pytest.skip("mysqld not available (set AVIDBACKUP_TEST_MYSQLD)")
    base = Path(exe).resolve().parent.parent
    root = tmp_path_factory.mktemp("mysqld")
    datadir = root / "data"
    init = subprocess.run(
        [exe, "--no-defaults", "--initialize-insecure", f"--datadir={datadir}", f"--basedir={base}"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if init.returncode != 0:
        pytest.skip(f"could not initialize a temporary MySQL: {init.stderr[-300:]}")
    port = _free_port()
    proc = subprocess.Popen(
        [
            exe, "--no-defaults", f"--datadir={datadir}", f"--basedir={base}", f"--port={port}",
            "--bind-address=127.0.0.1", "--mysqlx=OFF", "--innodb-buffer-pool-size=64M",
            f"--log-error={root / 'error.log'}", f"--pid-file={root / 'mysqld.pid'}",
            f"--tmpdir={root}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(port):
            pytest.skip("temporary MySQL did not start")
        for _ in range(40):
            try:
                pymysql.connect(host="127.0.0.1", port=port, user="root", password="").close()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            pytest.skip("temporary MySQL does not accept connections")
        mysql_client = next(
            (str(p) for p in (base / "bin" / "mysql.exe", base / "bin" / "mysql") if p.is_file()), None
        )
        yield SimpleNamespace(host="127.0.0.1", port=port, user="root", password="", client=mysql_client)
    finally:
        # Ask the server to stop itself; terminate() alone can leave mysqld running on Windows.
        try:
            pymysql.connect(
                host="127.0.0.1", port=port, user="root", password="", autocommit=True
            ).cursor().execute("SHUTDOWN")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def connect(server, database: str | None = None):  # noqa: ANN001, ANN201
    import pymysql

    return pymysql.connect(
        host=server.host, port=server.port, user=server.user, password=server.password,
        database=database, autocommit=True, charset="utf8mb4",
    )


def seed_database(server, name: str = "avb_src") -> str:  # noqa: ANN001
    """Create a database with awkward data: quotes, unicode, NULLs, binary, many rows, a view, a
    trigger and a procedure."""
    conn = connect(server)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
    cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    cur.execute(f"USE `{name}`")
    cur.execute(
        "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), note TEXT, "
        "score DECIMAL(10,3), ratio DOUBLE, born DATE, seen DATETIME, flags BIT(8), meta JSON) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    cur.executemany(
        "INSERT INTO users (name, note, score, ratio, born, seen, flags, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            ("O'Brien", 'He said "hi" \\ backslash', "12.500", 0.1, "1990-02-03", "2026-11-20 10:11:12", 5, '{"a": [1, 2]}'),
            ("Zoë 日本語 🙂", "line1\nline2\ttab", "-0.001", 1e-9, None, None, 0, None),
            ("", None, None, None, "2000-01-01", "2000-01-01 00:00:00", 255, "{}"),
        ],
    )
    cur.execute("CREATE TABLE blobs (id INT PRIMARY KEY, data BLOB, fixed BINARY(4), blank BLOB)")
    cur.execute(
        "INSERT INTO blobs VALUES (1, %s, %s, %s), (2, NULL, NULL, NULL)",
        (bytes(range(256)) + b"\x00\x00\xff'\"\\", b"\x00\x01\x02\x03", b""),
    )
    cur.execute("CREATE TABLE big (id INT PRIMARY KEY, payload VARCHAR(200)) ENGINE=InnoDB")
    cur.executemany(
        "INSERT INTO big VALUES (%s, %s)", [(i, f"row {i} " + "x" * (i % 50)) for i in range(1, 2501)]
    )
    cur.execute("CREATE TABLE audit (id INT AUTO_INCREMENT PRIMARY KEY, what VARCHAR(50))")
    cur.execute("CREATE TABLE `odd``name` (`a b` INT)")
    cur.execute("INSERT INTO `odd``name` VALUES (7)")
    cur.execute("CREATE VIEW adults AS SELECT id, name FROM users WHERE born IS NOT NULL")
    cur.execute(
        "CREATE TRIGGER users_ai AFTER INSERT ON users FOR EACH ROW INSERT INTO audit (what) VALUES (NEW.name)"
    )
    cur.execute("CREATE PROCEDURE bump(IN n INT) BEGIN UPDATE big SET payload = CONCAT(payload, n) WHERE id = 1; END")
    cur.execute("INSERT INTO users (name) VALUES ('Late')")  # fires the trigger: audit has 1 row
    conn.close()
    return name


def table_checksums(server, database: str) -> dict[str, int | None]:  # noqa: ANN001
    conn = connect(server, database)
    cur = conn.cursor()
    cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
    tables = [row[0] for row in cur.fetchall()]
    out: dict[str, int | None] = {}
    for table in tables:
        cur.execute(f"CHECKSUM TABLE `{table.replace('`', '``')}`")
        out[table] = cur.fetchone()[1]
    conn.close()
    return out


class PhpSite:
    """A PHP built-in web server serving one folder."""

    def __init__(self, php: str, webroot: Path, tmp: Path) -> None:
        self.webroot = webroot
        self.port = _free_port()
        env = {**os.environ, "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp)}
        self.proc = subprocess.Popen(
            [php, "-S", f"127.0.0.1:{self.port}", "-t", str(webroot)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not _wait_for_port(self.port, 20):
            self.stop()
            pytest.skip("php built-in server did not start")

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def php_site(php_exe: str, tmp_path: Path):
    sites: list[PhpSite] = []

    def start(files: dict[str, str]) -> PhpSite:
        webroot = tmp_path / f"www{len(sites)}"
        webroot.mkdir()
        for name, text in files.items():
            (webroot / name).write_text(text, encoding="utf-8")
        tmp = tmp_path / "phptmp"
        tmp.mkdir(exist_ok=True)
        site = PhpSite(php_exe, webroot, tmp)
        sites.append(site)
        return site

    yield start
    for site in sites:
        site.stop()
