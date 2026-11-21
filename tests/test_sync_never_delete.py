from __future__ import annotations

from ftplib import error_perm
from pathlib import Path, PurePosixPath

from avidbackup.config import Settings
from avidbackup.remote import RemoteClient, RemoteEntry
from avidbackup.sync_files import sync_changed_files


class FakeRemote(RemoteClient):
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
        out: list[RemoteEntry] = []
        prefix = (remote_dir.rstrip("/") or "") 
        for path, data in self.files.items():
            parent = str(PurePosixPath(path).parent)
            if parent == prefix or parent == remote_dir:
                out.append(
                    RemoteEntry(
                        path=path,
                        name=PurePosixPath(path).name,
                        size=len(data),
                        mtime=100.0,
                        is_dir=False,
                    )
                )
        return out

    def download_file(self, remote_path, local_path, progress=None):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.files[remote_path])
        if progress:
            progress(len(self.files[remote_path]))

    def upload_bytes(self, remote_path, data):
        raise NotImplementedError

    def read_bytes(self, remote_path, max_bytes=2_000_000):
        return self.files[remote_path][:max_bytes]

    def delete_file(self, remote_path):
        raise NotImplementedError

    def exists(self, remote_path):
        return remote_path in self.files


def _settings(root: Path) -> Settings:
    return Settings(
        protocol="ftp",
        host="example.com",
        port=21,
        user="u",
        password="p",
        tls_explicit=False,
        passive=True,
        remote_files_root="/public_html",
        local_backup_root=root,
        local_files_dir=root / "files",
        local_mysql_dir=root / "mysql",
        incremental=False,
        prune_local=False,
        download_retries=1,
    )


def test_remote_deleted_file_stays_local(tmp_path: Path, monkeypatch):
    root = tmp_path / "acct"
    root.mkdir()
    settings = _settings(root)
    settings.local_files_dir.mkdir(parents=True)
    kept = settings.local_files_dir / "gone-on-server.txt"
    kept.write_text("keep me", encoding="utf-8")

    # First sync downloads index.html only; gone-on-server is not on remote
    remote = FakeRemote({"/public_html/index.html": b"hi"})

    # Patch connect so download retries use the same fake client
    monkeypatch.setattr(
        "avidbackup.sync_files.connect",
        lambda _s: remote,
    )

    sync_changed_files(settings, client=remote, account_id="ex")
    # Exact server hierarchy: /public_html/index.html → files/public_html/index.html
    assert (settings.local_files_dir / "public_html" / "index.html").is_file()
    assert kept.is_file()
    assert kept.read_text(encoding="utf-8") == "keep me"

    # Second sync: remote deletes nothing local; still empty of gone-on-server
    remote2 = FakeRemote({"/public_html/index.html": b"hi"})
    monkeypatch.setattr("avidbackup.sync_files.connect", lambda _s: remote2)
    settings.incremental = True
    sync_changed_files(settings, client=remote2, account_id="ex")
    assert kept.is_file()


def test_home_root_backs_up_upload_and_app_outside_public_html(tmp_path: Path, monkeypatch):
    """Default remote root `/` must keep changed files above public_html."""
    root = tmp_path / "acct"
    root.mkdir()
    settings = _settings(root)
    settings.remote_files_root = "/"
    settings.incremental = False
    settings.skip_paths = ["cache", "tmp", "temp", ".git", "node_modules", ".trash"]

    class HomeFake(FakeRemote):
        def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
            # Build a tiny tree: / → upload/, app/, public_html/ (+ files)
            tree: dict[str, list[RemoteEntry]] = {
                "/": [
                    RemoteEntry("/upload", "upload", 0, None, True),
                    RemoteEntry("/app", "app", 0, None, True),
                    RemoteEntry("/public_html", "public_html", 0, None, True),
                    RemoteEntry("/.trash", ".trash", 0, None, True),
                ],
                "/upload": [
                    RemoteEntry("/upload/photo.jpg", "photo.jpg", 4, 1.0, False),
                ],
                "/app": [
                    RemoteEntry("/app/bootstrap.php", "bootstrap.php", 3, 1.0, False),
                ],
                "/public_html": [
                    RemoteEntry("/public_html/index.html", "index.html", 2, 1.0, False),
                ],
                "/.trash": [
                    RemoteEntry("/.trash/old.bin", "old.bin", 1, 1.0, False),
                ],
            }
            key = remote_dir.rstrip("/") or "/"
            return list(tree.get(key, []))

        def download_file(self, remote_path, local_path, progress=None):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(self.files[remote_path])

        def exists(self, remote_path):
            return remote_path in self.files or remote_path in {
                "/upload", "/app", "/public_html", "/.trash",
            }

    files = {
        "/upload/photo.jpg": b"img!",
        "/app/bootstrap.php": b"<?@",
        "/public_html/index.html": b"ok",
        "/.trash/old.bin": b"x",
    }
    remote = HomeFake(files)
    monkeypatch.setattr("avidbackup.sync_files.connect", lambda _s: remote)
    stats = sync_changed_files(settings, client=remote, account_id="ex")
    assert (settings.local_files_dir / "upload" / "photo.jpg").read_bytes() == b"img!"
    assert (settings.local_files_dir / "app" / "bootstrap.php").read_bytes() == b"<?@"
    assert (settings.local_files_dir / "public_html" / "index.html").read_bytes() == b"ok"
    assert not (settings.local_files_dir / ".trash" / "old.bin").exists()
    assert stats.downloaded == 3

    # Change detection: only the updated upload is re-fetched (size/mtime change).
    settings.incremental = True
    files["/upload/photo.jpg"] = b"IMG2!"

    class HomeFakeChanged(HomeFake):
        def list_dir(self, remote_dir: str) -> list[RemoteEntry]:
            entries = super().list_dir(remote_dir)
            out: list[RemoteEntry] = []
            for e in entries:
                if e.path == "/upload/photo.jpg":
                    out.append(
                        RemoteEntry(e.path, e.name, len(files[e.path]), 99.0, False)
                    )
                else:
                    out.append(e)
            return out

    remote2 = HomeFakeChanged(files)
    monkeypatch.setattr("avidbackup.sync_files.connect", lambda _s: remote2)
    stats2 = sync_changed_files(settings, client=remote2, account_id="ex")
    assert stats2.downloaded == 1
    assert (settings.local_files_dir / "upload" / "photo.jpg").read_bytes() == b"IMG2!"


def test_permission_denied_does_not_fail_the_run(tmp_path: Path, monkeypatch):
    root = tmp_path / "acct"
    root.mkdir()
    settings = _settings(root)

    class Deny(FakeRemote):
        def download_file(self, remote_path, local_path, progress=None):
            if remote_path.endswith("blocked.php"):
                raise error_perm("550 Can't open blocked.php: Permission denied")
            super().download_file(remote_path, local_path, progress)

    remote = Deny(
        {
            "/public_html/index.html": b"ok",
            "/public_html/blocked.php": b"nope",
        }
    )
    monkeypatch.setattr("avidbackup.sync_files.connect", lambda _s: remote)
    stats = sync_changed_files(settings, client=remote, account_id="ex")
    assert stats.failed == 0
    assert stats.warnings == 1
    assert stats.downloaded == 1
    assert (settings.local_files_dir / "public_html" / "index.html").read_bytes() == b"ok"
    assert not (settings.local_files_dir / "public_html" / "blocked.php").exists()
