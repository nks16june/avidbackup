from __future__ import annotations

from pathlib import Path

import pytest

from avidbackup.secure_cli import resolve_secret
from avidbackup.store import (
    AccountConfig,
    DatabaseConfig,
    FtpConfig,
    load_config,
    save_config,
    upsert_account,
)


def test_roundtrip(backup_home: Path):
    cfg = load_config()
    acc = AccountConfig(
        id="example.com",
        label="Example",
        ftp=FtpConfig(host="ftp.example.com", user="u", password="secret"),
        databases=[
            DatabaseConfig(id="main", type="mysql", host="10.0.0.1", user="u", password="db", database="app")
        ],
    )
    upsert_account(cfg, acc)
    save_config(cfg)
    cfg2 = load_config()
    assert cfg2.account("example.com").ftp.host == "ftp.example.com"
    assert cfg2.account("example.com").databases[0].type == "mysql"
    assert cfg2.version >= 3


def test_password_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AVIDBACKUP_TEST_PASS", "from-env")
    assert resolve_secret("literal", "AVIDBACKUP_TEST_PASS") == "from-env"
    assert resolve_secret("literal", "") == "literal"
    with pytest.raises(ValueError):
        resolve_secret("", "MISSING_ENV_VAR_XYZ")
