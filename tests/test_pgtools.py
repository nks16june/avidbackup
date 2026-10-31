from __future__ import annotations

import hashlib
import io
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from avidbackup import pgtools
from avidbackup.pgtools import HttpRangeFile, PgToolsError, install_postgres_tools

FILES = {
    "pgsql/bin/pg_dump.exe": b"MZ-fake-pg_dump" * 1000,
    "pgsql/bin/libpq.dll": b"fake-libpq" * 50000,
    "pgsql/server_license.txt": b"PostgreSQL License\n",
    "pgsql/pgAdmin 4/big.bin": b"x" * 3_000_000,
}


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in FILES.items():
            z.writestr(name, data)
    return buf.getvalue()


def _entry(zip_path: str, *, bad_hash: bool = False) -> dict:
    data = FILES[zip_path]
    sha = hashlib.sha256(b"tampered" if bad_hash else data).hexdigest()
    return {
        "name": zip_path.rsplit("/", 1)[-1],
        "zip_path": zip_path,
        "size": len(data),
        "sha256": sha,
    }


@pytest.fixture
def server():
    payload = _zip_bytes()
    state = {"honour_range": True, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_GET(self):  # noqa: N802
            rng = self.headers.get("Range")
            state["requests"].append(rng)
            if rng and state["honour_range"]:
                m = re.match(r"bytes=(\d+)-(\d+)", rng)
                start, end = int(m.group(1)), min(int(m.group(2)), len(payload) - 1)
                body = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            else:
                body = payload
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/pg.zip"
    state["size"] = len(payload)
    state["payload"] = payload
    yield state
    httpd.shutdown()


def _manifest(server, *, bad_hash: bool = False) -> dict:
    return {
        "version": "0.0-test",
        "url": server["url"],
        "zip_size": server["size"],
        "source_page": "test",
        "files": [_entry("pgsql/bin/pg_dump.exe", bad_hash=bad_hash), _entry("pgsql/bin/libpq.dll")],
        "licenses": [_entry("pgsql/server_license.txt")],
    }


def test_shipped_manifest_is_well_formed():
    man = pgtools.load_manifest()
    names = {f["name"] for f in man["files"]}
    assert {"pg_dump.exe", "psql.exe", "pg_restore.exe", "libpq.dll"} <= names
    for entry in [*man["files"], *man["licenses"]]:
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert "/" not in entry["name"] and "\\" not in entry["name"]
        assert entry["zip_path"].startswith("pgsql/")
    assert man["url"].startswith("https://")


def test_range_file_matches_source_and_fetches_partially(server):
    rf = HttpRangeFile(server["url"], server["size"])
    rf.seek(-22, 2)
    assert rf.read(22) == server["payload"][-22:]
    rf.seek(1_000_000 // 2)
    assert rf.read(5000) == server["payload"][500_000:505_000]


def test_install_downloads_only_needed_files(server, tmp_path, monkeypatch):
    monkeypatch.setattr(pgtools.sys, "platform", "win32")
    dest = install_postgres_tools(root=tmp_path, manifest=_manifest(server))
    assert (dest / "pg_dump.exe").read_bytes() == FILES["pgsql/bin/pg_dump.exe"]
    assert (dest / "libpq.dll").is_file()
    assert (dest / "server_license.txt").is_file()
    assert not any(p.name.endswith(".part") for p in dest.iterdir())
    assert dest == pgtools.tools_bin_dir("0.0-test", tmp_path)


def test_checksum_mismatch_refuses_to_install(server, tmp_path, monkeypatch):
    monkeypatch.setattr(pgtools.sys, "platform", "win32")
    with pytest.raises(PgToolsError, match="SHA256 mismatch"):
        install_postgres_tools(root=tmp_path, manifest=_manifest(server, bad_hash=True))
    dest = pgtools.tools_bin_dir("0.0-test", tmp_path)
    assert not (dest / "pg_dump.exe").exists()
    assert not any(p.name.endswith(".part") for p in dest.iterdir())


def test_server_without_range_support_is_rejected(server, tmp_path, monkeypatch):
    monkeypatch.setattr(pgtools.sys, "platform", "win32")
    monkeypatch.setattr(HttpRangeFile, "__init__", _fast_fail_init(HttpRangeFile.__init__))
    server["honour_range"] = False
    with pytest.raises(PgToolsError):
        install_postgres_tools(root=tmp_path, manifest=_manifest(server))


def _fast_fail_init(orig):
    def init(self, url, size, *, timeout=5.0, retries=1):
        orig(self, url, size, timeout=timeout, retries=1)

    return init


def test_non_windows_gets_package_manager_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(pgtools.sys, "platform", "linux")
    with pytest.raises(PgToolsError, match="package manager"):
        install_postgres_tools(root=tmp_path)


def test_managed_tools_are_preferred_over_path(monkeypatch, tmp_path: Path):
    from avidbackup import util_bin

    monkeypatch.setenv("AVIDBACKUP_HOME", str(tmp_path))
    bin_dir = pgtools.tools_bin_dir(root=tmp_path)
    bin_dir.mkdir(parents=True)
    (bin_dir / "pg_dump.exe").write_bytes(b"x")
    assert util_bin.find_pg_dump() == str(bin_dir / "pg_dump.exe")
