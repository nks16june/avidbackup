from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


@contextmanager
def mysql_defaults_file(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
) -> Iterator[Path]:
    """
    Write a temporary MySQL option file (mode 0600) for CLI tools.
    Prefer --defaults-extra-file over --password= on the command line.
    """
    tmp_dir = tempfile.mkdtemp(prefix="avidbackup-mysql-")
    path = Path(tmp_dir) / "my.cnf"
    # Escape backslashes and quotes for option-file safety
    safe_pass = password.replace("\\", "\\\\").replace('"', '\\"')
    content = (
        "[client]\n"
        f"host={host}\n"
        f"port={int(port)}\n"
        f"user={user}\n"
        f'password="{safe_pass}"\n'
    )
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass


@contextmanager
def pgpass_file(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> Iterator[Path]:
    """Temporary .pgpass-style file for PostgreSQL tools (mode 0600)."""
    tmp_dir = tempfile.mkdtemp(prefix="avidbackup-pg-")
    path = Path(tmp_dir) / "pgpass"
    # hostname:port:database:username:password
    line = f"{host}:{int(port)}:{database}:{user}:{password}\n"
    path.write_text(line, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass


def resolve_secret(literal: str, env_name: str = "") -> str:
    """Prefer password_env when set; otherwise use the literal password."""
    name = (env_name or "").strip()
    if name:
        val = os.environ.get(name)
        if val is None or val == "":
            raise ValueError(f"Environment variable {name!r} is not set for password_env")
        return val
    return literal or ""
