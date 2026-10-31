from __future__ import annotations

import sys

from avidbackup import procutil


def test_cli_command_source_install(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert procutil.cli_command("run", "--account", "x") == [
        sys.executable, "-m", "avidbackup", "run", "--account", "x",
    ]


def test_cli_command_frozen_exe_has_no_dash_m(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert procutil.cli_command("run") == [sys.executable, "run"]
