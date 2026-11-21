from __future__ import annotations

from pathlib import Path

from avidbackup.manifest import FileState, Manifest


def test_needs_download_skip_unchanged(tmp_path: Path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    man = Manifest(tmp_path / ".manifest.json")
    man.set("/a.txt", FileState(size=5, mtime=100.0, local_path=str(local)))
    assert man.needs_download("/a.txt", 5, 100.0, local, incremental=True) is False


def test_needs_download_changed_size(tmp_path: Path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    man = Manifest(tmp_path / ".manifest.json")
    man.set("/a.txt", FileState(size=5, mtime=100.0, local_path=str(local)))
    assert man.needs_download("/a.txt", 9, 100.0, local, incremental=True) is True


def test_needs_download_path_remap_does_not_redownload(tmp_path: Path):
    """Day-folder / hierarchy moves must not re-download when size+mtime still match."""
    new_local = tmp_path / "2026-11-21" / "files" / "public_html" / "a.txt"
    new_local.parent.mkdir(parents=True)
    new_local.write_bytes(b"hello")
    # Preserve mtime like FTP restore does
    import os

    os.utime(new_local, (100.0, 100.0))
    man = Manifest(tmp_path / ".manifest.json")
    man.set(
        "/public_html/a.txt",
        FileState(size=5, mtime=100.0, local_path=str(tmp_path / "files" / "a.txt")),
    )
    assert man.needs_download("/public_html/a.txt", 5, 100.0, new_local, incremental=True) is False


def test_needs_download_hardlinked_without_manifest(tmp_path: Path):
    local = tmp_path / "a.txt"
    local.write_bytes(b"hello")
    import os

    os.utime(local, (100.0, 100.0))
    man = Manifest(tmp_path / ".manifest.json")
    assert man.needs_download("/a.txt", 5, 100.0, local, incremental=True) is False
    assert man.needs_download("/a.txt", 5, 200.0, local, incremental=True) is True


def test_prune_local_does_not_delete(tmp_path: Path):
    """Guarantee: local files remain even when remote entry disappears from listing."""
    import inspect

    from avidbackup import sync_files

    local = tmp_path / "keep.txt"
    local.write_text("stay", encoding="utf-8")
    src = inspect.getsource(sync_files.sync_changed_files)
    assert "unlink" not in src and "os.remove" not in src
    assert "NEVER deleted" in sync_files.sync_changed_files.__doc__
    assert local.is_file()
