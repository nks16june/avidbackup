from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avidbackup.paths import app_root, logs_dir

ROOT = app_root()


@dataclass
class Settings:
    """Runtime settings for one backup run (built from accounts.json via settings_builder)."""

    protocol: str
    host: str
    port: int
    user: str
    password: str
    tls_explicit: bool
    passive: bool
    remote_files_root: str
    local_backup_root: Path
    local_files_dir: Path
    local_mysql_dir: Path
    incremental: bool
    prune_local: bool = False
    skip_paths: list[str] = field(default_factory=list)
    daily_at: str = "02:00"
    log_level: str = "INFO"
    site_url: str = ""
    mysql_config_candidates: list[str] = field(
        default_factory=lambda: [
            # Expanded at runtime via expand_mysql_config_candidates() /
            # public_html prefixes + deep scan. Keep a short explicit list here
            # for backwards-compatible Settings serialization.
            "wp-config.php",
            "configuration.php",
            ".env",
            "includes/config.php",
            "include/config.php",
            "config.php",
            "db.php",
            "connection.php",
            "app/config/database.php",
            "application/config/database.php",
            "public_html/include/config.php",
            "public_html/includes/config.php",
            "public_html/wp-config.php",
            "public_html/configuration.php",
            "public_html/.env",
        ]
    )
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_profile: str = "web"
    mysql_dump_timeout: int = 7200
    mysql_dump_via_php: bool = False
    mysql_try_direct: bool = True
    mysql_ssl: bool = False
    max_files: int = 0
    download_retries: int = 3
    large_table_mb: float = 50.0
    mysql_retention_days: int = 15
    compress_dumps: str = "gzip"
    databases: list[Any] = field(default_factory=list)
    verify_tls: bool = True
    host_key_policy: str = "tofu"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787

    manifest_path: Path = field(init=False)
    log_dir: Path = field(init=False)
    history_db: Path = field(init=False)
    env_path: Path = field(init=False, default_factory=lambda: Path())

    def __post_init__(self) -> None:
        self.manifest_path = self.local_backup_root / ".manifest.json"
        self.log_dir = logs_dir()
        self.history_db = self.local_backup_root / "history.sqlite3"
