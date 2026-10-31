from __future__ import annotations

import platform
import sys
from pathlib import Path

from avidbackup.paths import app_root, ensure_layout
from avidbackup.util_bin import find_mysql, find_mysqldump, find_pg_dump, find_psql


def run_doctor() -> int:
    ensure_layout()
    root = app_root()
    rows: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 10)
    rows.append(
        (
            "Python >= 3.10",
            py_ok,
            f"{platform.python_version()} ({sys.executable})",
        )
    )
    rows.append(("App home writable", _writable(root), str(root)))
    rows.append(("config/ writable", _writable(root / "config"), str(root / "config")))
    rows.append(("data/ writable", _writable(root / "data"), str(root / "data")))
    rows.append(("logs/ writable", _writable(root / "logs"), str(root / "logs")))

    md = find_mysqldump()
    rows.append(("mysqldump", bool(md), md or "not found (PyMySQL fallback OK)"))
    my = find_mysql()
    rows.append(("mysql client", bool(my), my or "optional"))
    pg = find_pg_dump()
    rows.append(("pg_dump", bool(pg), pg or "only needed for PostgreSQL; run: avidbackup tools install-postgres"))
    psql = find_psql()
    rows.append(("psql", bool(psql), psql or "optional for Postgres table sizes"))

    try:
        import flask  # noqa: F401

        rows.append(("Flask", True, "installed"))
    except ImportError:
        rows.append(("Flask", False, "pip install flask"))

    try:
        import paramiko  # noqa: F401

        rows.append(("paramiko (SFTP)", True, "installed"))
    except ImportError:
        rows.append(("paramiko (SFTP)", False, "pip install paramiko"))

    required = {"Python >= 3.10", "App home writable", "config/ writable", "data/ writable", "Flask"}
    width = max(len(r[0]) for r in rows)
    print("AvidBackup doctor")
    print("-" * (width + 40))
    failed = 0
    for name, ok, detail in rows:
        if ok:
            mark = "PASS"
        elif name in required:
            mark = "FAIL"
            failed += 1
        else:
            mark = "WARN"
        print(f"{name:<{width}}  {mark}  {detail}")
    print("-" * (width + 40))
    return 1 if failed else 0


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False
