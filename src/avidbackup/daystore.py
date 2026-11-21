"""One folder per account per day: files/ and db/, so a day can be restored on its own.

Unchanged files are hard-linked from the previous day (or the older files/ mirror).
Replacing a file in today's folder does not change yesterday's copy.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from avidbackup.retention import DUMP_GLOBS

log = logging.getLogger(__name__)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Local files mirror the FTP account home (public_html/, app/, upload/, …).
# Older days flattened public_html into files/ — do not hard-link those forward.
FILES_LAYOUT_VERSION = "account-home-v1"


def day_stamp(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y-%m-%d")


def iter_day_names(account_root: Path) -> list[str]:
    if not account_root.is_dir():
        return []
    names = [p.name for p in account_root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)]
    return sorted(names)


def dump_directories(account_root: Path) -> list[Path]:
    """Legacy mysql/ plus every day's db/ folder."""
    found: list[Path] = []
    legacy = account_root / "mysql"
    if legacy.is_dir():
        found.append(legacy)
    for name in iter_day_names(account_root):
        db_dir = account_root / name / "db"
        if db_dir.is_dir():
            found.append(db_dir)
    return found


def _seed_source(account_root: Path, day: str) -> Path | None:
    earlier = [name for name in iter_day_names(account_root) if name < day]
    if earlier:
        candidate = account_root / earlier[-1] / "files"
        if candidate.is_dir():
            return candidate
    legacy = account_root / "files"
    if legacy.is_dir():
        return legacy
    return None


def link_tree(src: Path, dst: Path) -> int:
    """Hard-link every file from src into dst. Existing destination files are left alone."""
    linked = 0
    skipped = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if name.endswith(".part"):
                continue
            src_file = Path(dirpath) / name
            if not src_file.is_file():
                continue
            dst_file = target_dir / name
            if dst_file.exists():
                skipped += 1
                continue
            try:
                os.link(src_file, dst_file)
                linked += 1
            except OSError as exc:
                log.warning("Could not link %s → %s: %s", src_file, dst_file, exc)
            if linked and linked % 5000 == 0:
                log.info("Linked %s files into %s", linked, dst)
    log.info("Link tree %s → %s linked=%s already_present=%s", src, dst, linked, skipped)
    return linked


def _link_same_day_dumps(account_root: Path, db_dir: Path, day: str) -> int:
    """Pull dumps written earlier today into the old mysql/ folder into this day's db/."""
    legacy = account_root / "mysql"
    if not legacy.is_dir():
        return 0
    placed = 0
    seen: set[Path] = set()
    for pattern in DUMP_GLOBS:
        for path in legacy.glob(pattern):
            if path in seen or not path.is_file() or path.name.startswith("."):
                continue
            seen.add(path)
            created = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
            if created != day:
                continue
            dest = db_dir / path.name
            if dest.exists():
                continue
            try:
                os.link(path, dest)
            except OSError:
                continue
            for suffix in (".sha256", ".done"):
                side = Path(str(path) + suffix)
                if side.is_file():
                    side_dest = Path(str(dest) + suffix)
                    if not side_dest.exists():
                        try:
                            os.link(side, side_dest)
                        except OSError:
                            pass
            placed += 1
    return placed


def prepare_day_dirs(account_root: Path, *, when: datetime | None = None) -> tuple[Path, Path]:
    """
    Create account/YYYY-MM-DD/files and account/YYYY-MM-DD/db.

    The first call of the day hard-links the previous day's files (or the legacy
    files/ mirror) so unchanged bytes are not copied again.
    """
    account_root.mkdir(parents=True, exist_ok=True)
    day = day_stamp(when)
    day_root = account_root / day
    files_dir = day_root / "files"
    db_dir = day_root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    marker = day_root / ".files_linked"
    layout_marker = day_root / ".files_layout"
    if not marker.is_file():
        files_dir.mkdir(parents=True, exist_ok=True)
        source = _seed_source(account_root, day)
        if source is not None and source.resolve() != files_dir.resolve():
            prev_layout = source.parent / ".files_layout"
            prev_ver = ""
            if prev_layout.is_file():
                try:
                    prev_ver = prev_layout.read_text(encoding="utf-8").strip()
                except OSError:
                    prev_ver = ""
            if prev_ver == FILES_LAYOUT_VERSION:
                link_tree(source, files_dir)
            else:
                log.info(
                    "Skipping hard-link from %s — previous day used a flat layout. "
                    "This day will store the exact server hierarchy "
                    "(public_html/, app/, upload/, …).",
                    source,
                )
        layout_marker.write_text(FILES_LAYOUT_VERSION + "\n", encoding="utf-8")
        marker.write_text(day + "\n", encoding="utf-8")
        log.info("Day folder ready: %s (layout=%s)", day_root, FILES_LAYOUT_VERSION)
    placed = _link_same_day_dumps(account_root, db_dir, day)
    if placed:
        log.info("Placed %s dump(s) from mysql/ into %s", placed, db_dir)
    return files_dir, db_dir
