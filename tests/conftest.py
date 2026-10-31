from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def backup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AVIDBACKUP_HOME", str(home))
    # Reset cached paths by ensuring modules read env fresh
    from avidbackup.paths import ensure_layout

    ensure_layout(home)
    return home
