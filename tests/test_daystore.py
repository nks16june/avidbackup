from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from avidbackup.daystore import FILES_LAYOUT_VERSION, link_tree, prepare_day_dirs


def test_day_folder_hardlinks_previous_files(tmp_path: Path):
    account = tmp_path / "site"
    monday_root = account / "2026-11-16"
    monday_files = monday_root / "files" / "public_html"
    monday_files.mkdir(parents=True)
    original = monday_files / "index.html"
    original.write_text("monday", encoding="utf-8")
    (monday_root / ".files_layout").write_text(FILES_LAYOUT_VERSION + "\n", encoding="utf-8")
    (monday_root / ".files_linked").write_text("2026-11-16\n", encoding="utf-8")
    (monday_root / "db").mkdir(parents=True, exist_ok=True)

    tuesday = datetime(2026, 11, 17, 3, 0, 0)
    files_dir, db_dir = prepare_day_dirs(account, when=tuesday)
    linked = files_dir / "public_html" / "index.html"
    assert linked.read_text(encoding="utf-8") == "monday"
    assert db_dir.is_dir()
    assert os.path.samefile(original, linked)
    assert (account / "2026-11-17" / ".files_layout").read_text(encoding="utf-8").strip() == FILES_LAYOUT_VERSION

    # Replacing today's file must not rewrite yesterday's bytes.
    replacement = linked.with_suffix(".html.new")
    replacement.write_text("tuesday", encoding="utf-8")
    os.replace(replacement, linked)
    assert original.read_text(encoding="utf-8") == "monday"
    assert linked.read_text(encoding="utf-8") == "tuesday"


def test_day_folder_skips_hardlink_from_flat_legacy(tmp_path: Path):
    account = tmp_path / "site"
    legacy = account / "files"
    legacy.mkdir(parents=True)
    (legacy / "index.html").write_text("flat-legacy", encoding="utf-8")

    day = datetime(2026, 11, 21, 3, 0, 0)
    files_dir, _db = prepare_day_dirs(account, when=day)
    # Must not copy flat public_html contents into the new hierarchy day.
    assert not (files_dir / "index.html").exists()
    assert (account / "2026-11-21" / ".files_layout").read_text(encoding="utf-8").strip() == FILES_LAYOUT_VERSION


def test_link_tree_skips_existing(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_text("new", encoding="utf-8")
    dst.mkdir()
    (dst / "a.txt").write_text("keep", encoding="utf-8")
    link_tree(src, dst)
    assert (dst / "a.txt").read_text(encoding="utf-8") == "keep"
