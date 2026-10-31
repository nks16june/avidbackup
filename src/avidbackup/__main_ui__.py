"""Windowed PyInstaller entry.

No arguments: `avidbackup app` (UI + browser + tray, no console).
With arguments: behaves like the `avidbackup` CLI (used for UI-started
backups and the Windows scheduled task, e.g. `avidbackup.exe run`).
"""

from __future__ import annotations

import os
import sys


def _ensure_std_streams() -> None:
    # A windowed exe has no console: sys.stdout/stderr are None.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main() -> None:
    _ensure_std_streams()
    if len(sys.argv) > 1:
        from avidbackup.main import run

        raise SystemExit(run(sys.argv[1:]))
    from avidbackup.desktop_app import run_app

    raise SystemExit(run_app(open_browser=True, no_tray=False))


if __name__ == "__main__":
    main()
