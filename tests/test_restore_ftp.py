from __future__ import annotations

from pathlib import Path, PurePosixPath

from avidbackup.config import Settings
from avidbackup.remote import RemoteClient, RemoteEntry
from avidbackup.restore_ftp import _local_rel_to_remote, restore_day_files_via_ftp


class FakeUp(RemoteClient):
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, bytes]] = []
        self.dirs: set[str] = set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
        return []

    def download_file(self, *a, **k):
        raise NotImplementedError

    def upload_bytes(self, remote_path: str, data: bytes) -> None:
        self.uploaded.append((remote_path, data))

    def ensure_dir(self, remote_dir: str) -> None:
        self.dirs.add(remote_dir)

    def upload_file(self, local_path: Path, remote_path: str, progress=None) -> None:
        parent = str(PurePosixPath(remote_path).parent)
        if parent not in {".", "/"}:
            self.ensure_dir(parent)
        self.uploaded.append((remote_path, local_path.read_bytes()))

    def read_bytes(self, *a, **k):
        return b""

    def delete_file(self, *a, **k):
        return None

    def exists(self, remote_path: str) -> bool:
        return False


def test_local_rel_to_remote_preserves_hierarchy():
    assert _local_rel_to_remote("public_html/index.php") == "/public_html/index.php"
    assert _local_rel_to_remote("app/bootstrap.php") == "/app/bootstrap.php"


def test_restore_uploads_account_home_paths(tmp_path: Path):
    files = tmp_path / "files"
    (files / "public_html").mkdir(parents=True)
    (files / "app").mkdir(parents=True)
    (files / "public_html" / "index.html").write_bytes(b"web")
    (files / "app" / "x.php").write_bytes(b"app")
    settings = Settings(
        protocol="ftp",
        host="example.com",
        port=21,
        user="u",
        password="p",
        tls_explicit=False,
        passive=True,
        remote_files_root="/",
        local_backup_root=tmp_path,
        local_files_dir=files,
        local_mysql_dir=tmp_path / "db",
        incremental=True,
    )
    client = FakeUp()
    stats = restore_day_files_via_ftp(settings, files, client=client)
    assert stats.uploaded == 2
    assert stats.failed == 0
    paths = {p for p, _ in client.uploaded}
    assert paths == {"/public_html/index.html", "/app/x.php"}
