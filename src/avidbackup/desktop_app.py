from __future__ import annotations

import logging
import os
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
from avidbackup.webui.security import is_loopback_host, resolve_ui_token, signin_url

log = logging.getLogger(__name__)


def run_app(
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
    no_tray: bool = False,
) -> int:
    """
    Start the local agent (management API + optional tray).

    The browser is only a client — closing a tab must not stop this process.
    If an agent is already running, open the UI and exit 0 (do not start a second copy).
    """
    from avidbackup.agent_service import agent_mode_requested, agent_ui_url

    ensure_layout()
    # Agent installs default to no browser; explicit --no-browser still wins.
    if agent_mode_requested() and os.environ.get("AVIDBACKUP_FORCE_BROWSER") != "1":
        # Keep open_browser if caller passed True only when not in agent argv mode…
        # `app --no-browser` sets open_browser=False from main already.
        pass

    try:
        acquire_app_lock(app_root())
    except RuntimeError:
        # System agent already up — browser is the client.
        url = agent_ui_url()
        print(f"AvidBackup agent already running -> {url}", file=sys.stderr)
        if open_browser and os.environ.get("AVIDBACKUP_NO_BROWSER") != "1":
            try:
                webbrowser.open(signin_url(url, resolve_ui_token(load_config().ui_token)))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open browser: %s", exc)
        return 0

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

    app = create_app(bind_host=bind_host)
    open_url = signin_url(url, resolve_ui_token(cfg.ui_token))
    server = _make_server(bind_host, listen_port, app)
    thread = threading.Thread(target=server.serve_forever, name="avidbackup-ui", daemon=True)
    thread.start()
    print(f"AvidBackup agent -> {url}")
    print(f"Home: {app_root()}")
    print("Browser is optional - closing the tab does not stop the agent or backups.")
    if resolve_ui_token(cfg.ui_token) and not open_browser:
        print("Sign-in is required. Run 'avidbackup token' to print your sign-in link.")

    if open_browser and os.environ.get("AVIDBACKUP_NO_BROWSER") != "1":

        def _open() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(open_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open browser: %s", exc)

        threading.Thread(target=_open, daemon=True).start()

    use_tray = not no_tray and os.environ.get("AVIDBACKUP_NO_TRAY") != "1"
    if use_tray and _try_tray(open_url, server):
        return 0

    # Console / headless agent: stay up until killed (Task Scheduler restarts it).
    print("Agent running (no tray). Press Ctrl+C only if you intend to stop the agent.")
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        print("\nShutting down agent...")
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
        try:
            from avidbackup.procutil import spawn_account_runs
            from avidbackup.store import load_config as _load

            enabled = [a.id for a in _load().enabled_accounts()]
            started = spawn_account_runs(enabled)
            log.info(
                "Started %s independent backup process(es): %s",
                len(started),
                ", ".join(f"{aid}:{pid}" for aid, pid in started),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Run all failed: %s", exc)

    def quit_app(icon: Any, item: Any = None) -> None:
        _shutdown(server)
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open UI", open_ui, default=True),
        pystray.MenuItem("Run all backups now", run_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop agent", quit_app),
    )
    icon = pystray.Icon("avidbackup", _icon_image(), "AvidBackup", menu)
    try:
        icon.run()
    finally:
        _shutdown(server)
    return True
