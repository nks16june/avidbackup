from __future__ import annotations

from avidbackup.config import Settings
from avidbackup.paths import logs_dir
from avidbackup.secure_cli import resolve_secret
from avidbackup.store import AccountConfig, AppConfig, DatabaseConfig, resolve_data_dir
from avidbackup.validation import validate_account_id


def settings_from_account(cfg: AppConfig, account: AccountConfig) -> Settings:
    """Build runtime Settings from accounts.json (single supported config format)."""
    validate_account_id(account.id)
    ftp = account.ftp
    account_root = resolve_data_dir(cfg, account)
    password = resolve_secret(ftp.password, ftp.password_env)
    from avidbackup.remote import normalize_access_protocol

    transport = normalize_access_protocol(ftp.protocol or "ftps")
    default_port = 22 if transport == "sftp" else 21
    settings = Settings(
        protocol=transport,  # ssh → sftp for the client
        host=ftp.host,
        port=int(ftp.port or default_port),
        user=ftp.user,
        password=password,
        tls_explicit=bool(ftp.tls_explicit),
        passive=bool(ftp.passive),
        remote_files_root=(ftp.remote_root or "/").rstrip("/") or "/",
        local_backup_root=account_root,
        local_files_dir=account_root / "files",
        local_mysql_dir=account_root / "mysql",
        incremental=True,
        prune_local=False,
        # Exact path-segment names only (see sync_files._should_skip). Never list
        # app / upload / uploads / storage / public_html — those stay in backup.
        skip_paths=["cache", "tmp", "temp", ".git", "node_modules"],
        site_url=account.site_url or f"https://{account.domain or account.id}",
        mysql_host="",
        mysql_port=3306,
        mysql_profile="web",
        download_retries=int(cfg.download_retries or 3),
        large_table_mb=float(cfg.large_table_mb or 50),
        dashboard_host=cfg.ui_host,
        dashboard_port=cfg.ui_port,
        verify_tls=bool(ftp.verify_tls),
        host_key_policy=(ftp.host_key_policy or "tofu").lower(),
        mysql_ssl=False,
    )
    # Whole-account home `/`: skip cPanel system dirs. Dot-folders (.cpanel,
    # .trash, …) are skipped in sync_files._should_skip; keep app/upload/storage.
    if settings.remote_files_root in {"/", ""}:
        settings.skip_paths = list(
            dict.fromkeys(
                [
                    *settings.skip_paths,
                    "mail",
                    "etc",
                    "ssl",
                    "access-logs",
                    "cgi-bin",
                    "tmp",
                    "temp",
                    "cache",
                ]
            )
        )
        # Prefer account-home env files for MySQL discovery (often above public_html).
        settings.mysql_config_candidates = [
            ".env",
            ".env_local",
            ".env.local",
            ".env.production",
            *settings.mysql_config_candidates,
        ]
    settings.manifest_path = account_root / ".manifest.json"
    settings.history_db = account_root / "history.sqlite3"
    settings.log_dir = logs_dir()
    settings.databases = list(account.databases)
    settings.mysql_retention_days = int(account.retention.mysql_days or cfg.mysql_retention_days)
    settings.compress_dumps = cfg.compress_dumps
    return settings


def explicit_databases(account: AccountConfig) -> list[DatabaseConfig]:
    return [
        d
        for d in account.databases
        if d.enabled and (d.database or d.path or d.discover_from_site)
    ]
