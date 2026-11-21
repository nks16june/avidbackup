"""Persistent local agent: keeps the UI/API running after install (Windows).

The browser is only a client. Closing a tab must not stop backups or the agent.
On Windows we register a logon Scheduled Task that restarts the agent if it dies.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

from avidbackup.locks import pid_alive
from avidbackup.paths import app_root
from avidbackup.procutil import cli_command

log = logging.getLogger(__name__)

AGENT_TASK_NAME = "AvidBackup\\Agent"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class AgentStatus:
    installed: bool
    task_name: str
    running: bool
    pid: int | None
    ui_url: str
    detail: str = ""


def agent_ui_url() -> str:
    try:
        from avidbackup.store import load_config

        cfg = load_config()
        host = (cfg.ui_host or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", ""}:
            host = "127.0.0.1"
        port = int(cfg.ui_port or 8787)
    except Exception:  # noqa: BLE001
        host, port = "127.0.0.1", 8787
    return f"http://{host}:{port}/"


def _agent_argv() -> list[str]:
    # --no-browser: agent is the system process; user opens UI when needed.
    return cli_command("app", "--no-browser")


def _running_agent_pid() -> int | None:
    lock = app_root() / ".avidbackup_app.lock"
    if not lock.is_file():
        return None
    try:
        pid = int(lock.read_text(encoding="utf-8").strip().splitlines()[0])
    except (ValueError, IndexError, OSError):
        return None
    return pid if pid_alive(pid) else None


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def agent_task_installed() -> bool:
    if sys.platform != "win32":
        return False
    proc = _schtasks("/Query", "/TN", AGENT_TASK_NAME)
    return proc.returncode == 0


def get_agent_status() -> AgentStatus:
    pid = _running_agent_pid()
    installed = agent_task_installed() if sys.platform == "win32" else False
    detail = ""
    if sys.platform != "win32":
        detail = "Agent autostart is implemented for Windows (logon task)."
    elif installed and not pid:
        detail = "Agent task is installed but not running — start it or sign in again."
    elif pid:
        detail = "Agent is running; browser is optional."
    else:
        detail = "Agent not installed. Run: avidbackup agent install"
    return AgentStatus(
        installed=installed,
        task_name=AGENT_TASK_NAME,
        running=bool(pid),
        pid=pid,
        ui_url=agent_ui_url(),
        detail=detail,
    )


def _ps_single(value: str) -> str:
    """Quote a value for PowerShell single-quoted string literals."""
    return "'" + value.replace("'", "''") + "'"


def install_agent(*, start_now: bool = True) -> AgentStatus:
    """Register logon autostart + failure restart; optionally start immediately."""
    if sys.platform != "win32":
        raise OSError("Agent install is only supported on Windows")

    root = app_root()
    argv = _agent_argv()
    exe = argv[0]
    args = subprocess.list2cmdline(argv[1:])
    # Hidden PowerShell registers a proper task (unlimited runtime, restart on fail).
    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_single(exe)} -Argument {_ps_single(args)} -WorkingDirectory {_ps_single(str(root))}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {_ps_single(AGENT_TASK_NAME)} -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null
"""
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise OSError(f"Could not install agent task: {err}")

    # Ensure child sees agent mode even if argv is rewritten later.
    env_marker = root / "config" / "agent.mode"
    try:
        env_marker.parent.mkdir(parents=True, exist_ok=True)
        env_marker.write_text("1\n", encoding="utf-8")
    except OSError:
        pass

    if start_now:
        start_agent()
    return get_agent_status()


def uninstall_agent() -> AgentStatus:
    if sys.platform != "win32":
        raise OSError("Agent uninstall is only supported on Windows")
    _schtasks("/Delete", "/TN", AGENT_TASK_NAME, "/F")
    marker = app_root() / "config" / "agent.mode"
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    return get_agent_status()


def start_agent() -> AgentStatus:
    """Start the agent now (via task if installed, else detached process)."""
    if _running_agent_pid():
        return get_agent_status()

    if sys.platform == "win32" and agent_task_installed():
        run = _schtasks("/Run", "/TN", AGENT_TASK_NAME)
        if run.returncode != 0:
            err = (run.stderr or run.stdout or "").strip()
            raise OSError(f"Could not start agent task: {err}")
        # Give the process a moment to take the lock.
        import time

        for _ in range(40):
            if _running_agent_pid():
                break
            time.sleep(0.25)
        return get_agent_status()

    from avidbackup.procutil import spawn_detached

    spawn_detached(_agent_argv(), cwd=app_root(), label="agent")
    import time

    for _ in range(40):
        if _running_agent_pid():
            break
        time.sleep(0.25)
    return get_agent_status()


def open_ui_or_start_agent(*, open_browser: bool = True) -> int:
    """
    Browser-as-client entry: if the agent is already up, only open the UI.
    Otherwise start the agent (and open the UI).
    """
    import webbrowser

    status = get_agent_status()
    if status.running:
        if open_browser:
            try:
                webbrowser.open(status.ui_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open browser: %s", exc)
        print(f"AvidBackup agent already running (pid={status.pid}) → {status.ui_url}")
        return 0

    if sys.platform == "win32" and not status.installed:
        try:
            status = install_agent(start_now=True)
        except OSError as exc:
            log.warning("Agent install failed (%s); starting once", exc)
            status = start_agent()
    else:
        status = start_agent()

    if open_browser:
        try:
            webbrowser.open(status.ui_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open browser: %s", exc)
    print(f"AvidBackup agent → {status.ui_url} (pid={status.pid})")
    return 0 if status.running or status.pid else 1


def agent_mode_requested() -> bool:
    if os.environ.get("AVIDBACKUP_AGENT", "").strip() in {"1", "true", "yes"}:
        return True
    return (app_root() / "config" / "agent.mode").is_file()
