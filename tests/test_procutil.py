from __future__ import annotations

import sys
import time
from pathlib import Path

from avidbackup import procutil


def test_cli_command_source_install(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert procutil.cli_command("run", "--account", "x") == [
        sys.executable, "-m", "avidbackup", "run", "--account", "x",
    ]


def test_cli_command_frozen_exe_has_no_dash_m(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert procutil.cli_command("run") == [sys.executable, "run"]


def test_spawn_account_runs_calls_per_account(monkeypatch):
    calls: list[tuple] = []

    def fake_spawn_cli(*args, label="", cwd=None):
        calls.append((args, label))
        return 1000 + len(calls)

    monkeypatch.setattr(procutil, "spawn_cli", fake_spawn_cli)
    out = procutil.spawn_account_runs(["a.com", "b.com"], extra_args=["--files-only"])
    assert out == [("a.com", 1001), ("b.com", 1002)]
    assert calls[0][0][:3] == ("run", "--account", "a.com")
    assert "--files-only" in calls[0][0]
    assert calls[0][1] == "a.com"
    assert calls[1][1] == "b.com"


def test_spawn_detached_survives_brief_parent(tmp_path: Path, monkeypatch):
    """Child must keep running after the spawning call returns (no wait)."""
    monkeypatch.setenv("AVIDBACKUP_HOME", str(tmp_path))
    # logs_dir() follows AVIDBACKUP_HOME via app_root when accounts.json exists
    (tmp_path / "accounts.json").write_text('{"accounts":[]}', encoding="utf-8")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "alive.txt"
    pid = procutil.spawn_detached(
        [sys.executable, "-c", f"import time; time.sleep(1.5); open(r'{marker}', 'w').write('ok')"],
        cwd=tmp_path,
        label="survive",
        log_file=tmp_path / "logs" / "spawn.log",
    )
    assert isinstance(pid, int) and pid >= 0
    deadline = time.time() + 20
    while time.time() < deadline and not marker.exists():
        time.sleep(0.25)
    assert marker.exists(), "detached child did not write marker (spawn failed?)"
    assert marker.read_text(encoding="utf-8") == "ok"
