"""Safe browse helpers for account/YYYY-MM-DD/{files,db} restore points."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from avidbackup.daystore import _DAY_RE, iter_day_names
from avidbackup.retention import DUMP_GLOBS

Kind = Literal["files", "db"]

@dataclass
class DirEntry:
    name: str
    rel: str
    is_dir: bool
    size: int = 0
    mtime: datetime | None = None


@dataclass
class DaySnapshot:
    day: str
    path: Path
    files_dir: Path | None
    db_dir: Path | None
    files_count: int = 0
    files_bytes: int = 0
    files_updated: int = 0  # new/changed files from history for this day
    dumps: list[dict] = field(default_factory=list)
    has_files: bool = False
    has_db: bool = False
    files_status: str = "na"  # ok | warn | fail | missed | na
    db_status: str = "na"


def parse_day(day: str) -> str:
    day = (day or "").strip()
    if not _DAY_RE.match(day):
        raise ValueError("Invalid day (expected YYYY-MM-DD)")
    return day


def normalize_rel(rel: str) -> str:
    """Normalize a relative path under files/; reject traversal."""
    raw = (rel or "").replace("\\", "/").strip("/")
    if not raw:
        return ""
    if ".." in raw.split("/"):
        raise ValueError("Path traversal not allowed")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError("Absolute paths not allowed")
    # Allow common web paths; still block null bytes / weirdness.
    if "\x00" in raw:
        raise ValueError("Invalid path")
    return raw


def day_root(account_root: Path, day: str) -> Path:
    return account_root / parse_day(day)


def resolve_under(root: Path, rel: str) -> Path | None:
    """Resolve rel under root; return None if it escapes."""
    try:
        root_r = root.resolve()
        target = (root / rel).resolve() if rel else root_r
        target.relative_to(root_r)
    except (OSError, ValueError):
        return None
    return target


def _count_tree(path: Path, *, max_files: int = 250_000) -> tuple[int, int]:
    """Return (file_count, total_bytes). Caps walk for huge trees."""
    import os

    n = 0
    total = 0
    if not path.is_dir():
        return 0, 0
    for dirpath, _dirs, filenames in os.walk(path):
        for name in filenames:
            if name.endswith(".part"):
                continue
            n += 1
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
            if n >= max_files:
                return n, total
    return n, total


_DUMP_STAMP_RE = re.compile(r"_(\d{8})_")


def _dump_entry(path: Path) -> dict | None:
    if not path.is_file() or path.name.startswith("."):
        return None
    if path.name.endswith((".sha256", ".done", ".part")):
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return {
        "name": path.name,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime),
        "checksum": Path(str(path) + ".sha256").is_file(),
        "path": path,
    }


def dump_day_key(path: Path) -> str | None:
    """
    Calendar day this dump belongs to.

    Prefer ``_YYYYMMDD_`` in the filename (mysqldump naming); else local mtime date.
    """
    m = _DUMP_STAMP_RE.search(path.name)
    if m:
        raw = m.group(1)
        try:
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        except Exception:  # noqa: BLE001
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return None


def list_dumps(db_dir: Path) -> list[dict]:
    if not db_dir.is_dir():
        return []
    seen: set[Path] = set()
    out: list[dict] = []
    for pattern in DUMP_GLOBS:
        for p in sorted(db_dir.glob(pattern)):
            if p in seen:
                continue
            seen.add(p)
            entry = _dump_entry(p)
            if entry:
                out.append(entry)
    out.sort(key=lambda d: d["name"])
    return out


def dumps_for_day(account_root: Path, day: str) -> list[dict]:
    """
    All dump files for a calendar day.

    Looks in ``YYYY-MM-DD/db/`` (current layout) and legacy ``mysql/`` (older runs).
    """
    day = parse_day(day)
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []

    def _add(path: Path) -> None:
        try:
            st = path.stat()
            key = (st.st_dev, st.st_ino)
        except OSError:
            return
        if key in seen:
            return
        entry = _dump_entry(path)
        if not entry:
            return
        seen.add(key)
        out.append(entry)

    day_db = account_root / day / "db"
    if day_db.is_dir():
        for pattern in DUMP_GLOBS:
            for p in day_db.glob(pattern):
                _add(p)

    legacy = account_root / "mysql"
    if legacy.is_dir():
        for pattern in DUMP_GLOBS:
            for p in legacy.glob(pattern):
                if dump_day_key(p) == day:
                    _add(p)

    out.sort(key=lambda d: d["name"])
    return out


def legacy_dump_days(account_root: Path) -> list[str]:
    """Distinct YYYY-MM-DD keys present only via legacy ``mysql/`` dumps."""
    legacy = account_root / "mysql"
    if not legacy.is_dir():
        return []
    days: set[str] = set()
    for pattern in DUMP_GLOBS:
        for p in legacy.glob(pattern):
            key = dump_day_key(p)
            if key and _DAY_RE.match(key):
                days.add(key)
    return sorted(days)


def snapshot_for_day(
    account_root: Path,
    day: str,
    *,
    count_files: bool = True,
) -> DaySnapshot | None:
    day = parse_day(day)
    root = day_root(account_root, day)
    files_dir = root / "files"
    db_dir = root / "db"
    files_ok = files_dir.is_dir()
    dumps = dumps_for_day(account_root, day)
    has_db = bool(dumps) or db_dir.is_dir()

    if not root.is_dir() and not dumps:
        return None

    fcount, fbytes = (0, 0)
    if files_ok and count_files:
        fcount, fbytes = _count_tree(files_dir, max_files=50_000)
    elif files_ok:
        # Fast path for calendar lists: top-level entry count only.
        try:
            fcount = sum(1 for p in files_dir.iterdir() if not p.name.startswith("."))
        except OSError:
            fcount = 0
    return DaySnapshot(
        day=day,
        path=root,
        files_dir=files_dir if files_ok else None,
        db_dir=db_dir if db_dir.is_dir() else None,
        files_count=fcount,
        files_bytes=fbytes,
        dumps=dumps,
        has_files=files_ok,
        has_db=has_db,
    )


def list_restore_days(account_root: Path) -> list[DaySnapshot]:
    names = set(iter_day_names(account_root))
    names.update(legacy_dump_days(account_root))
    out: list[DaySnapshot] = []
    for day in sorted(names, reverse=True):
        snap = snapshot_for_day(account_root, day, count_files=False)
        if snap and (snap.has_files or snap.dumps or snap.has_db):
            # Empty day/db folders without dumps do not count as restore points.
            if snap.has_files or snap.dumps:
                out.append(snap)
    return out


def list_directory(files_root: Path, rel: str = "") -> tuple[str, Path, list[DirEntry]]:
    """One-level listing under files_root/rel. Returns (rel, abs_path, entries)."""
    rel_n = normalize_rel(rel)
    target = resolve_under(files_root, rel_n)
    if target is None or not target.is_dir():
        raise FileNotFoundError(rel_n or ".")
    entries: list[DirEntry] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise FileNotFoundError(str(exc)) from exc
    for child in children:
        if child.name.startswith(".") or child.name.endswith(".part"):
            continue
        child_rel = f"{rel_n}/{child.name}" if rel_n else child.name
        child_rel = child_rel.replace("\\", "/")
        is_dir = child.is_dir()
        size = 0
        mtime: datetime | None = None
        try:
            st = child.stat()
            mtime = datetime.fromtimestamp(st.st_mtime)
            if not is_dir:
                size = st.st_size
        except OSError:
            pass
        entries.append(DirEntry(name=child.name, rel=child_rel, is_dir=is_dir, size=size, mtime=mtime))
    return rel_n, target, entries


def resolve_download(account_root: Path, day: str, kind: Kind, rel: str) -> Path:
    """Resolve a downloadable file under day's files/, day db/, or legacy mysql/."""
    day = parse_day(day)
    rel_n = normalize_rel(rel)
    if kind == "db":
        # DB downloads are single filenames (day/db or legacy mysql/).
        if "/" in rel_n or "\\" in rel_n:
            raise ValueError("DB dump path must be a single filename")
        for base in (account_root / day / "db", account_root / "mysql"):
            if not base.is_dir():
                continue
            target = resolve_under(base, rel_n)
            if target is not None and target.is_file():
                # Legacy mysql/: only allow if dump belongs to this calendar day.
                if base.name == "mysql" and dump_day_key(target) != day:
                    continue
                return target
        raise FileNotFoundError(rel_n)

    base = account_root / day / "files"
    if not base.is_dir():
        raise FileNotFoundError(kind)
    target = resolve_under(base, rel_n)
    if target is None or not target.is_file():
        raise FileNotFoundError(rel_n)
    return target
