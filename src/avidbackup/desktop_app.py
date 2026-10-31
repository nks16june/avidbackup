from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

from avidbackup.locks import acquire_app_lock
from avidbackup.netutil import find_free_port
from avidbackup.paths import app_root, ensure_layout
from avidbackup.store import load_config
from avidbackup.webui.app import create_app
from avidbackup.webui.security import is_loopback_host

log = logging.getLogger(__name__)


def run_app(
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
    no_tray: bool = False,
) -> int:
    """
    Start the management UI, optionally open a browser and a system tray.
    Entry point for `avidbackup app` and the PyInstaller windowed launcher.
    """
    ensure_layout()
    try:
        acquire_app_lock(app_root())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    cfg = load_config()
    host = (host or cfg.ui_host or "127.0.0.1").strip()
    preferred = int(port if port is not None else (cfg.ui_port or 8787))
    token = (cfg.ui_token or os.environ.get("AVIDBACKUP_UI_TOKEN") or "").strip()

    if not is_loopback_host(host):
        if not token:
            print(
                f"ERROR: Refusing to bind UI to non-loopback host {host!r} without "
                "ui_token / AVIDBACKUP_UI_TOKEN.",
                file=sys.stderr,
            )
            return 2
        print(
            f"WARNING: Binding AvidBackup UI to {host} (non-loopback). "
            "UI token required.",
            file=sys.stderr,
        )

    bind_host = "127.0.0.1" if host in {"localhost", ""} else host
    listen_port = find_free_port(preferred, host=bind_host if ":" not in bind_host else "127.0.0.1")
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{listen_port}/"

    app = create_app()
    server = _make_server(bind_host, listen_port, app)
    thread = threading.Thread(target=server.serve_forever, name="avidbackup-ui", daemon=True)
    thread.start()
    print(f"AvidBackup UI -> {url}")
    print(f"Home: {app_root()}")

    if open_browser and os.environ.get("AVIDBACKUP_NO_BROWSER") != "1":

        def _open() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open browser: %s", exc)

        threading.Thread(target=_open, daemon=True).start()

    use_tray = not no_tray and os.environ.get("AVIDBACKUP_NO_TRAY") != "1"
    if use_tray and _try_tray(url, server):
        return 0

    # Console fallback: block until Ctrl+C / server stop
    print("Tray unavailable (install with: pip install 'avidbackup[desktop]'). Running in console mode.")
    print("Press Ctrl+C to quit.")
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        _shutdown(server)
    return 0


def _make_server(host: str, port: int, app: Any):
    from werkzeug.serving import make_server

    return make_server(host, port, app, threaded=True)


def _shutdown(server: Any) -> None:
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _try_tray(url: str, server: Any) -> bool:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    def _icon_image() -> Any:
        img = Image.new("RGB", (64, 64), color=(15, 76, 129))
        draw = ImageDraw.Draw(img)
        draw.rectangle((12, 12, 52, 52), outline=(255, 255, 255), width=3)
        draw.rectangle((22, 28, 42, 48), fill=(255, 255, 255))
        return img

    def open_ui(icon: Any = None, item: Any = None) -> None:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Open UI failed: %s", exc)

    def run_all(icon: Any = None, item: Any = None) -> None:
        def _job() -> None:
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "avidbackup", "run"],
                    cwd=str(app_root()),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Run all failed: %s", exc)

        threading.Thread(target=_job, daemon=True).start()

    def quit_app(icon: Any, item: Any = None) -> None:
        _shutdown(server)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open UI", open_ui, default=True),
        pystray.MenuItem("Run all backups now", run_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("avidbackup", _icon_image(), "AvidBackup", menu)
    try:
        icon.run()
    finally:
        _shutdown(server)
    return True
