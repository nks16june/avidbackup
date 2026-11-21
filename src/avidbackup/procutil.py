"""Helpers for launching AvidBackup's own CLI as a child process."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from avidbackup.paths import app_root, logs_dir

log = logging.getLogger(__name__)

# Hide console windows for helper processes on Windows (schtasks, powershell probes).
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


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


def _spawn_log_path(label: str = "run") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (label or "run"))
    return logs_dir() / f"spawn_{safe}_{stamp}.log"


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label or "run").strip("._-")[:48] or "run"


def _find_pid_containing(fragment: str, *, timeout: float = 15.0) -> int | None:
    if not fragment:
        return None
    frag = fragment.replace("'", "''")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -match 'python' -and $_.CommandLine -and "
                    f"$_.CommandLine -like '*{frag}*' }} | "
                    "Select-Object -First 1 -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
            text = (proc.stdout or "").strip()
            if text.isdigit():
                return int(text)
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.4)
    return None


def _vbs_quote(path: Path | str) -> str:
    """Double any quotes for embedding inside a VBScript string literal."""
    return str(path).replace('"', '""')


def _spawn_windows_task(argv: list[str], *, cwd: Path, out_path: Path, label: str) -> int:
    """
    Launch through a persistent Scheduled Task.

    Cursor/agent Job Objects kill ordinary child processes within seconds. A
    Task Scheduler job is started by the system and keeps running after the UI
    or agent shell exits. The task definition is kept (updated with /F) so
    each Start backup reuses AvidBackup\\Run-<label>.

    Task action is wscript → hidden cmd (window style 0) running python.exe with
    stdout/stderr appended to the spawn log. Do not register cmd.exe or
    pythonw.exe as the task action itself (visible console / silent failures).
    """
    safe = _safe_label(label)
    task_name = f"AvidBackup\\Run-{safe}"
    scripts = logs_dir() / "spawn_scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    cmd_path = scripts / f"run_{safe}.cmd"
    vbs_path = scripts / f"run_{safe}.vbs"
    # Keep python.exe (not pythonw) so logging and PID discovery work.
    exe_line = subprocess.list2cmdline(argv)
    cmd_path.write_text(
        "@echo off\r\n"
        f'cd /d "{cwd}"\r\n'
        f"echo ----- %DATE% %TIME% ----->> \"{out_path}\"\r\n"
        f"{exe_line} >> \"{out_path}\" 2>&1\r\n",
        encoding="utf-8",
    )
    vbs_path.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{_vbs_quote(cwd)}"\r\n'
        f'sh.Run "cmd.exe /c ""{_vbs_quote(cmd_path)}""", 0, True\r\n',
        encoding="utf-8",
    )
    tr = subprocess.list2cmdline(
        ["wscript.exe", "//B", "//Nologo", str(vbs_path)]
    )

    create = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            task_name,
            "/SC",
            "ONCE",
            "/ST",
            "23:59",
            "/SD",
            "01/01/2099",
            "/TR",
            tr,
            "/RL",
            "LIMITED",
            "/F",
            "/IT",
        ],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if create.returncode != 0:
        err = (create.stderr or create.stdout or "").strip()
        raise OSError(f"schtasks /Create failed ({create.returncode}): {err}")

    run = subprocess.run(
        ["schtasks", "/Run", "/TN", task_name],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if run.returncode != 0:
        err = (run.stderr or run.stdout or "").strip()
        raise OSError(f"schtasks /Run failed ({run.returncode}): {err}")

    # Prefer the account id / last args — more stable than a long cmdline needle.
    needles = []
    if label:
        needles.append(str(label))
    if len(argv) >= 3:
        needles.append(" ".join(argv[-3:]))
    needles.append("avidbackup run")
    pid = 0
    for needle in needles:
        pid = _find_pid_containing(needle, timeout=8.0) or 0
        if pid:
            break
    if not pid:
        # Surface a real failure instead of "started (pid 0)".
        tail = ""
        try:
            if out_path.is_file():
                tail = out_path.read_text(encoding="utf-8", errors="replace")[-800:]
        except OSError:
            pass
        raise OSError(
            f"Scheduled task {task_name} ran but no backup process was found. "
            f"Check {out_path}"
            + (f"\n---\n{tail}" if tail else "")
        )
    log.info("Scheduled task %s started pid=%s log=%s", task_name, pid, out_path)
    return int(pid)


def _spawn_posix(argv: list[str], *, cwd: Path, out_path: Path, env: dict) -> int:
    out_f = out_path.open("a", encoding="utf-8")
    try:
        out_f.write(f"\n--- spawn {datetime.now().isoformat(timespec='seconds')} ---\n")
        out_f.write("cmd: " + " ".join(argv) + "\n")
        out_f.flush()
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        try:
            out_f.close()
        except OSError:
            pass
    return int(proc.pid)


_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_windows_direct(
    argv: list[str], *, cwd: Path, out_path: Path, env: dict[str, str]
) -> int:
    """Plain detached child: survives the parent, writes to the spawn log, returns its PID."""
    base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    last: OSError | None = None
    with out_path.open("ab") as out:
        # Leaving the parent's job object only works if the job allows it; retry without.
        for extra in (_CREATE_BREAKAWAY_FROM_JOB, 0):
            try:
                proc = subprocess.Popen(  # noqa: S603
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    creationflags=base | extra,
                    close_fds=True,
                )
                return proc.pid
            except OSError as exc:
                last = exc
    raise last or OSError("could not start the process")


def _spawn_windows(
    argv: list[str],
    *,
    cwd: Path,
    out_path: Path,
    label: str,
    env: dict[str, str],
    mode: str | None = None,
) -> int:
    """
    Choose how to start a detached process on Windows.

    AVIDBACKUP_SPAWN_MODE: ``auto`` (default: detached process, Scheduled Task as fallback),
    ``direct`` (never use Task Scheduler) or ``task`` (always use Task Scheduler).
    """
    chosen = (mode or os.environ.get("AVIDBACKUP_SPAWN_MODE", "auto")).strip().lower()
    if chosen == "task":
        return _spawn_windows_task(argv, cwd=cwd, out_path=out_path, label=label)
    try:
        return _spawn_windows_direct(argv, cwd=cwd, out_path=out_path, env=env)
    except OSError as exc:
        if chosen == "direct":
            raise
        log.warning("Detached start failed (%s); falling back to a Scheduled Task", exc)
        return _spawn_windows_task(argv, cwd=cwd, out_path=out_path, label=label)


def spawn_detached(
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    label: str = "run",
    log_file: Path | None = None,
) -> int:
    """
    Start a process that keeps running after the parent exits.

    Windows: a detached process (own process group, outside the parent's job object when
    allowed). If that cannot be started, or AVIDBACKUP_SPAWN_MODE=task, a persistent
    Scheduled Task is used instead (needed when an editor or terminal kills child processes).
    Unix: start_new_session.
    """
    work = Path(cwd) if cwd is not None else app_root()
    out_path = Path(log_file) if log_file is not None else _spawn_log_path(label)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    with out_path.open("a", encoding="utf-8") as out_f:
        out_f.write(f"\n--- spawn {datetime.now().isoformat(timespec='seconds')} ---\n")
        out_f.write("cmd: " + " ".join(argv) + "\n")
        out_f.write(f"cwd: {work}\n")

    if sys.platform == "win32":
        return _spawn_windows(argv, cwd=work, out_path=out_path, label=label, env=env)
    return _spawn_posix(argv, cwd=work, out_path=out_path, env=env)


def spawn_cli(*args: str, label: str = "", cwd: Path | str | None = None) -> int:
    """Detached `avidbackup <args>` (or frozen equivalent)."""
    tag = label or ("-".join(args[:2]) if args else "cli")
    return spawn_detached(cli_command(*args), cwd=cwd, label=tag)


def spawn_account_runs(
    account_ids: list[str],
    *,
    extra_args: list[str] | None = None,
    cwd: Path | str | None = None,
) -> list[tuple[str, int]]:
    """
    Start one independent OS process per account (own lock, log, scheduled task).

    Used by Backup all so sites never share a process or thread pool.
    """
    extra = list(extra_args or [])
    started: list[tuple[str, int]] = []
    for account_id in account_ids:
        pid = spawn_cli(
            "run",
            "--account",
            account_id,
            *extra,
            label=account_id,
            cwd=cwd,
        )
        started.append((account_id, pid))
    return started
