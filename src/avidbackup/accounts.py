"""Compatibility aliases — accounts.json via store.py is the only config format."""

from __future__ import annotations

from avidbackup.settings_builder import settings_from_account
from avidbackup.store import (
    AccountConfig,
    AppConfig,
    load_config,
    resolve_data_dir,
    save_config,
)

Account = AccountConfig
AccountRegistry = AppConfig


def load_registry(path=None) -> AppConfig:
    return load_config(path)


def settings_for_account(account: AccountConfig, registry: AppConfig):
    return settings_from_account(registry, account)


__all__ = [
    "Account",
    "AccountConfig",
    "AccountRegistry",
    "AppConfig",
    "load_registry",
    "load_config",
    "save_config",
    "settings_for_account",
    "settings_from_account",
    "resolve_data_dir",
]
