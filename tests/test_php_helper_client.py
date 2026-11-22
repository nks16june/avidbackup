"""PHP helper: generator and client behaviour (no PHP or MySQL needed)."""

from __future__ import annotations

import gzip
import json
import re
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from avidbackup.php_helper import (
    DUMP_MARKER,
    HelperClient,
    HelperError,
    generate_helper,
    helper_filename,
    helper_url_for,
    install_helper,
    new_token,
    remove_helper,
    token_sha256,
)

TOKEN = "T0ken-" + "a" * 40
SQL = b"-- AvidBackup PHP helper dump\nCREATE TABLE t (id int);\nINSERT INTO t VALUES (1);\n" + DUMP_MARKER + b" on x\n"


class _Fake:
    """A scriptable fake helper endpoint."""

    def __init__(self, handler) -> None:  # noqa: ANN001
        outer = handler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                outer(self, form)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/avidbackup-test.php"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def _reply(h, code, body: bytes, ctype="application/json", extra=None):  # noqa: ANN001
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


@pytest.fixture
def fake():
    servers: list[_Fake] = []

    def make(handler):  # noqa: ANN001, ANN202
        s = _Fake(handler)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def _ok_handler(h, form):  # noqa: ANN001
    if form.get("token") != TOKEN:
        return _reply(h, 403, json.dumps({"ok": False, "error": "Token rejected"}).encode())
    action = form.get("action")
    if action == "dump":
        return _reply(h, 200, gzip.compress(SQL), "application/octet-stream", {"X-AvidBackup-Format": "gzip"})
    if action == "tables":
        return _reply(h, 200, json.dumps({"ok": True, "tables": [{"table": "t"}]}).encode())
    return _reply(h, 200, json.dumps({"ok": True, "version": "1", "tables": 3, "database": "d"}).encode())


# ---- generator ----------------------------------------------------------------------------


def test_generated_helper_has_hash_and_never_the_token():
    src = generate_helper(token=TOKEN, db_host="localhost", db_port=3306, db_user="u",
                          db_password="p", db_name="d")
    assert token_sha256(TOKEN) in src
    assert TOKEN not in src
    assert "__AVB_" not in src
    assert "define('AVB_DB_NAME', 'd');" in src
    assert "define('AVB_DB_PORT', 3306);" in src


def test_awkward_credentials_are_escaped_for_php():
    src = generate_helper(token=TOKEN, db_host="h", db_port=3306, db_user="us'er",
                          db_password="pa'ss\\w\"$rd{$x}", db_name="d'b")
    assert "define('AVB_DB_USER', 'us\\'er');" in src
    assert "define('AVB_DB_PASS', 'pa\\'ss\\\\w\"$rd{$x}');" in src
    assert "define('AVB_DB_NAME', 'd\\'b');" in src


def test_generation_rejects_weak_or_missing_input():
    with pytest.raises(HelperError, match="too short"):
        generate_helper(token="short", db_host="h", db_port=3306, db_user="u", db_password="p", db_name="d")
    with pytest.raises(HelperError, match="database name"):
        generate_helper(token=TOKEN, db_host="h", db_port=3306, db_user="u", db_password="p", db_name="")


def test_tokens_and_file_names_are_random():
    assert new_token() != new_token() and len(new_token()) >= 40
    names = {helper_filename() for _ in range(50)}
    assert len(names) == 50
    assert all(re.fullmatch(r"avidbackup-[0-9a-f]{16}\.php", n) for n in names)


def test_helper_url_is_built_from_the_site_url():
    assert helper_url_for("https://example.com/", "a.php") == "https://example.com/a.php"
    with pytest.raises(HelperError, match="site URL"):
        helper_url_for("", "a.php")


# ---- URL rules ----------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://example.com/a.php", "ftp://example.com/a.php", "example.com/a.php", ""])
def test_only_https_urls_are_accepted(url):
    with pytest.raises(HelperError):
        HelperClient(url, TOKEN)


@pytest.mark.parametrize("url", ["https://example.com/a.php", "http://127.0.0.1:8080/a.php", "http://localhost/a.php"])
def test_https_and_loopback_urls_are_accepted(url):
    HelperClient(url, TOKEN)


def test_a_token_is_required():
    with pytest.raises(HelperError, match="token"):
        HelperClient("https://example.com/a.php", "")


# ---- responses ----------------------------------------------------------------------------


def test_ping_check_and_tables(fake):
    server = fake(_ok_handler)
    client = HelperClient(server.url, TOKEN)
    assert client.ping()["ok"] is True
    assert client.check()["tables"] == 3
    assert client.tables() == [{"table": "t"}]


def test_rejected_token_is_explained(fake):
    server = fake(_ok_handler)
    with pytest.raises(HelperError, match="403.*rejected"):
        HelperClient(server.url, "wrong-token-" + "b" * 30).ping()


def test_missing_helper_is_explained(fake):
    server = fake(lambda h, f: _reply(h, 404, b"<html>not found</html>", "text/html"))
    with pytest.raises(HelperError, match="no helper was found"):
        HelperClient(server.url, TOKEN).ping()


def test_lockout_is_explained(fake):
    server = fake(lambda h, f: _reply(h, 429, json.dumps({"ok": False, "error": "Too many failed attempts"}).encode()))
    with pytest.raises(HelperError, match="429"):
        HelperClient(server.url, TOKEN).ping()


def test_a_non_helper_page_is_not_mistaken_for_one(fake):
    server = fake(lambda h, f: _reply(h, 200, b"<html>welcome</html>", "text/html"))
    with pytest.raises(HelperError, match="did not return an AvidBackup helper response"):
        HelperClient(server.url, TOKEN).ping()


def test_redirects_are_refused_because_they_would_drop_the_token(fake):
    server = fake(lambda h, f: _reply(h, 301, b"", "text/html", {"Location": "https://example.com/elsewhere"}))
    with pytest.raises(HelperError, match="redirects"):
        HelperClient(server.url, TOKEN).ping()


def test_unreachable_helper_is_reported():
    with pytest.raises(HelperError, match="Could not reach"):
        HelperClient("http://127.0.0.1:1/a.php", TOKEN, timeout=3).ping()


# ---- dump download --------------------------------------------------------------------------


def test_gzip_dump_is_decompressed_to_plain_sql(fake, tmp_path: Path):
    server = fake(_ok_handler)
    dest = tmp_path / "d.sql"
    written = HelperClient(server.url, TOKEN).dump_to(dest)
    assert dest.read_bytes() == SQL and written == len(SQL)
    assert not list(tmp_path.glob("*.part"))


def test_plain_dump_is_accepted(fake, tmp_path: Path):
    server = fake(lambda h, f: _reply(h, 200, SQL, "application/octet-stream"))
    dest = tmp_path / "d.sql"
    HelperClient(server.url, TOKEN).dump_to(dest)
    assert dest.read_bytes() == SQL


def test_truncated_download_is_discarded(fake, tmp_path: Path):
    cut = gzip.compress(SQL * 2000)[:-40]
    server = fake(lambda h, f: _reply(h, 200, cut, "application/octet-stream"))
    dest = tmp_path / "d.sql"
    with pytest.raises(HelperError, match="cut off|incomplete"):
        HelperClient(server.url, TOKEN).dump_to(dest)
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


def test_dump_without_the_completion_marker_is_discarded(fake, tmp_path: Path):
    server = fake(lambda h, f: _reply(h, 200, gzip.compress(b"CREATE TABLE t (id int);\n"), "application/octet-stream"))
    dest = tmp_path / "d.sql"
    with pytest.raises(HelperError, match="incomplete"):
        HelperClient(server.url, TOKEN).dump_to(dest)
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


def test_corrupt_gzip_is_discarded(fake, tmp_path: Path):
    bad = bytearray(gzip.compress(SQL * 500))
    bad[40:60] = b"\x00" * 20
    server = fake(lambda h, f: _reply(h, 200, bytes(bad), "application/octet-stream"))
    with pytest.raises((HelperError, zlib.error)):
        HelperClient(server.url, TOKEN).dump_to(tmp_path / "d.sql")
    assert not (tmp_path / "d.sql").exists() and not list(tmp_path.glob("*.part"))


def test_error_reply_on_dump_is_reported(fake, tmp_path: Path):
    server = fake(lambda h, f: _reply(h, 200, json.dumps({"ok": False, "error": "db down"}).encode()))
    with pytest.raises(HelperError, match="db down"):
        HelperClient(server.url, TOKEN).dump_to(tmp_path / "d.sql")


# ---- installing / removing ------------------------------------------------------------------


class _Remote:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def upload_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def delete_file(self, path: str) -> None:
        self.files.pop(path, None)


def test_install_uploads_a_randomly_named_helper_and_verifies_it(fake):
    server = fake(_ok_handler)
    remote = _Remote()
    base = server.url.rsplit("/", 1)[0]
    info = install_helper(remote, web_root="/public_html", site_url=base, token=TOKEN,
                          db_host="localhost", db_port=3306, db_user="u", db_password="p", db_name="d")
    assert re.fullmatch(r"/public_html/avidbackup-[0-9a-f]{16}\.php", info.path)
    assert info.url == f"{base}/{info.path.rsplit('/', 1)[1]}"
    uploaded = remote.files[info.path].decode()
    assert token_sha256(TOKEN) in uploaded and TOKEN not in uploaded


def test_install_removes_the_file_again_when_it_cannot_be_reached(fake):
    server = fake(lambda h, f: _reply(h, 404, b"nope", "text/html"))
    remote = _Remote()
    base = server.url.rsplit("/", 1)[0]
    with pytest.raises(HelperError, match="did not answer.*removed again"):
        install_helper(remote, web_root="/www", site_url=base, token=TOKEN, db_host="h", db_port=3306,
                       db_user="u", db_password="p", db_name="d", verify_attempts=1)
    assert remote.files == {}


def test_install_refuses_plain_http_site_urls():
    with pytest.raises(HelperError, match="https"):
        install_helper(_Remote(), web_root="/www", site_url="http://example.com", token=TOKEN, db_host="h",
                       db_port=3306, db_user="u", db_password="p", db_name="d")


def test_remove_deletes_the_recorded_file():
    remote = _Remote()
    remote.files["/www/a.php"] = b"x"
    remove_helper(remote, "/www/a.php")
    assert remote.files == {}
    with pytest.raises(HelperError, match="No helper path"):
        remove_helper(remote, "")
