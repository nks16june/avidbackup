"""Windowed PyInstaller entry: `avidbackup app` (UI + browser, no console)."""

from __future__ import annotations


def main() -> None:
    from avidbackup.desktop_app import run_app

    raise SystemExit(run_app(open_browser=True, no_tray=False))


if __name__ == "__main__":
    main()
