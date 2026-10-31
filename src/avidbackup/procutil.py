"""Helpers for launching AvidBackup's own CLI as a child process."""

from __future__ import annotations

import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def cli_command(*args: str) -> list[str]:
    """
    Command line that runs `avidbackup <args>` in a child process.

    Source/pip installs: `python -m avidbackup <args>`.
    PyInstaller exe: `sys.executable` is the bundled program itself (the
    windowed launcher), which dispatches CLI arguments, so no `-m` there.
    """
    if is_frozen():
        return [sys.executable, *args]
    return [sys.executable, "-m", "avidbackup", *args]
