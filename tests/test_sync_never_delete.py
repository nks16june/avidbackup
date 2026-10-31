from __future__ import annotations

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
    assert (settings.local_files_dir / "index.html").is_file()
    assert kept.is_file()
    assert kept.read_text(encoding="utf-8") == "keep me"

    # Second sync: remote deletes nothing local; still empty of gone-on-server
    remote2 = FakeRemote({"/public_html/index.html": b"hi"})
    monkeypatch.setattr("avidbackup.sync_files.connect", lambda _s: remote2)
    settings.incremental = True
    sync_changed_files(settings, client=remote2, account_id="ex")
    assert kept.is_file()
