from __future__ import annotations

import os
from pathlib import Path

# Packaged code lives in src/avidbackup → default project root is repo root when editable.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def app_root() -> Path:
    """
    Install-anywhere root for config/data/logs.

    Priority:
      1. AVIDBACKUP_HOME
      2. Editable checkout root (parent of src/) when present
      3. ~/.avidbackup
    """
    env = os.environ.get("AVIDBACKUP_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # Editable / git checkout: <root>/src/avidbackup
    if (_PACKAGE_ROOT / "pyproject.toml").is_file() or (_PACKAGE_ROOT / "accounts.json").is_file():
        return _PACKAGE_ROOT.resolve()
    return (Path.home() / ".avidbackup").resolve()


def ensure_layout(root: Path | None = None) -> Path:
    root = (root or app_root()).resolve()
    for sub in ("config", "config/accounts", "data", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def accounts_file(root: Path | None = None) -> Path:
    return (root or app_root()) / "accounts.json"


def data_root(root: Path | None = None) -> Path:
    return (root or app_root()) / "data"


def logs_dir(root: Path | None = None) -> Path:
    return (root or app_root()) / "logs"


def known_hosts_path(root: Path | None = None) -> Path:
    return (root or ensure_layout()) / "config" / "known_hosts"


def ui_secret_key_path(root: Path | None = None) -> Path:
    return (root or ensure_layout()) / "config" / "ui_secret_key"
