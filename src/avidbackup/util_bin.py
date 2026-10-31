from __future__ import annotations

import shutil
from pathlib import Path


def which_first(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_mysqldump() -> str | None:
    found = which_first("mysqldump")
    if found:
        return found
    for candidate in (
        "/usr/bin/mysqldump",
        "/usr/local/bin/mysqldump",
        "/opt/homebrew/bin/mysqldump",
        "/opt/local/bin/mysqldump",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def find_mysql() -> str | None:
    found = which_first("mysql")
    if found:
        return found
    for candidate in (
        "/usr/bin/mysql",
        "/usr/local/bin/mysql",
        "/opt/homebrew/bin/mysql",
        "/opt/local/bin/mysql",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def find_pg_dump() -> str | None:
    return which_first("pg_dump") or _path_if_exists(
        "/usr/bin/pg_dump",
        "/usr/local/bin/pg_dump",
        "/opt/homebrew/bin/pg_dump",
    )


def find_psql() -> str | None:
    return which_first("psql") or _path_if_exists(
        "/usr/bin/psql",
        "/usr/local/bin/psql",
        "/opt/homebrew/bin/psql",
    )


def _path_if_exists(*paths: str) -> str | None:
    for p in paths:
        if Path(p).is_file():
            return p
    return None
