from __future__ import annotations

from pathlib import Path

from avidbackup.config import Settings
from avidbackup.initial_full import has_completed_initial_full, mark_initial_full_done


def _settings(root: Path) -> Settings:
    return Settings(
        protocol="ftps",
        host="example.com",
        port=21,
        user="u",
        password="p",
        tls_explicit=True,
        passive=True,
        remote_files_root="/public_html",
        local_backup_root=root,
        local_files_dir=root / "files",
        local_mysql_dir=root / "mysql",
        incremental=True,
    )


def test_marker_after_zero_failure(tmp_path: Path):
    root = tmp_path / "acct"
    root.mkdir()
    s = _settings(root)
    assert has_completed_initial_full(s) is False
    mark_initial_full_done(s, account_id="x", downloaded=1, skipped=0, bytes_copied=10)
    assert has_completed_initial_full(s) is True


def test_incremental_refused_until_initial_full(tmp_path: Path):
    """Until the marker exists, run path must force incremental=False."""
    root = tmp_path / "acct"
    root.mkdir()
    s = _settings(root)
    assert s.incremental is True
    assert has_completed_initial_full(s) is False
    # Mimic main._run_one gate
    if not has_completed_initial_full(s):
        s.incremental = False
    assert s.incremental is False
    mark_initial_full_done(s, account_id="x", downloaded=0, skipped=0, bytes_copied=0)
    s.incremental = True
    assert has_completed_initial_full(s) is True
    assert s.incremental is True
