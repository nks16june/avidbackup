from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from avidbackup.paths import accounts_file, data_root, ensure_layout
from avidbackup.validation import validate_account_id

log = logging.getLogger(__name__)

CONFIG_VERSION = 3


def _slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return s or f"site-{secrets.token_hex(3)}"


@dataclass
class FtpConfig:
    protocol: str = "ftps"
    host: str = ""
    port: int = 21
    user: str = ""
    password: str = ""
    password_env: str = ""
    tls_explicit: bool = True
    passive: bool = True
    remote_root: str = "/"  # whole FTP/cPanel account home (not only public_html)
    verify_tls: bool = True
    host_key_policy: str = "tofu"  # tofu | strict | insecure


@dataclass
class DatabaseConfig:
    id: str
    label: str = ""
    type: str = "mysql"  # mysql | postgres | sqlite
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    password_env: str = ""
    database: str = ""
    path: str = ""  # sqlite local or remote path
    format: str = "plain"  # postgres: plain | custom
    compress: str = "gzip"  # gzip | none
    enabled: bool = True
    discover_from_site: bool = False
    ssl: bool = False
    # How AvidBackup reaches the database:
    #   direct - connect to host:port from this machine (default)
    #   ssh    - through an SSH tunnel (hosts that block remote MySQL but allow SSH)
    #   php    - through a small PHP helper uploaded to the site (no SSH, no remote MySQL)
    via: str = "direct"
    # --- via: ssh ---
    ssh_same_as_ftp: bool = False  # reuse the site's SFTP login (host, port, user, password)
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_password_env: str = ""
    ssh_key_file: str = ""
    ssh_key_passphrase_env: str = ""
    ssh_host_key_policy: str = "tofu"  # tofu | strict | insecure
    # --- via: php ---
    helper_url: str = ""
    helper_token: str = ""
    helper_token_env: str = ""
    helper_path: str = ""  # where the helper was uploaded on the server (for removal)


@dataclass
class RetentionConfig:
    mysql_days: int = 15  # kept name for compatibility = DB dump days
    files_forever: bool = True
    files_days: int = 0


@dataclass
class NotifyConfig:
    webhook_url: str = ""
    webhook_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_password_env: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_tls: bool = True
    # Off by default: phone alerts go through a third-party server (ntfy.sh unless you
    # point ntfy_server at your own). Opt in under Settings.
    phone_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    # Public ntfy.sh only gets a generic message unless this is on.
    ntfy_details: bool = False


@dataclass
class AccountConfig:
    id: str
    label: str
    domain: str = ""
    site_url: str = ""
    enabled: bool = True
    ftp: FtpConfig = field(default_factory=FtpConfig)
    databases: list[DatabaseConfig] = field(default_factory=list)
    retention: RetentionConfig = field(default_factory=RetentionConfig)


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    data_root: str = ""
    mysql_retention_days: int = 15
    files_keep_forever: bool = True
    large_table_mb: float = 50.0
    download_retries: int = 3
    compress_dumps: str = "gzip"
    ui_host: str = "127.0.0.1"
    ui_port: int = 8787
    ui_token: str = ""
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    accounts: list[AccountConfig] = field(default_factory=list)

    def account(self, account_id: str) -> AccountConfig:
        aid = validate_account_id(account_id)
        for a in self.accounts:
            if a.id == aid:
                return a
        raise KeyError(f"Unknown account id: {aid}")

    def enabled_accounts(self) -> list[AccountConfig]:
        return [a for a in self.accounts if a.enabled]


def _default_port(db_type: str) -> int:
    return {"mysql": 3306, "postgres": 5432, "sqlite": 0}.get(db_type, 3306)


def _ftp_from(raw: dict[str, Any] | None) -> FtpConfig:
    raw = raw or {}
    proto = str(raw.get("protocol") or "ftps").lower()
    default_port = 22 if proto == "sftp" else 21
    return FtpConfig(
        protocol=proto,
        host=str(raw.get("host") or ""),
        port=int(raw.get("port") or default_port),
        user=str(raw.get("user") or ""),
        password=str(raw.get("password") or ""),
        password_env=str(raw.get("password_env") or ""),
        tls_explicit=bool(raw.get("tls_explicit", True)),
        passive=bool(raw.get("passive", True)),
        remote_root=str(raw.get("remote_root") or "/"),
        verify_tls=bool(raw.get("verify_tls", True)),
        host_key_policy=str(raw.get("host_key_policy") or "tofu").lower(),
    )


def _db_from(raw: dict[str, Any]) -> DatabaseConfig:
    db_type = str(raw.get("type") or "mysql").lower()
    if db_type == "mariadb":
        db_type = "mysql"
    return DatabaseConfig(
        id=str(raw.get("id") or _slug(str(raw.get("database") or raw.get("label") or "db"))),
        label=str(raw.get("label") or raw.get("database") or raw.get("id") or "Database"),
        type=db_type,
        host=str(raw.get("host") or ""),
        port=int(raw.get("port") or _default_port(db_type)),
        user=str(raw.get("user") or ""),
        password=str(raw.get("password") or ""),
        password_env=str(raw.get("password_env") or ""),
        database=str(raw.get("database") or ""),
        path=str(raw.get("path") or ""),
        format=str(raw.get("format") or "plain"),
        compress=str(raw.get("compress") or "gzip"),
        enabled=bool(raw.get("enabled", True)),
        discover_from_site=bool(raw.get("discover_from_site", False)),
        ssl=bool(raw.get("ssl", False)),
        via=_via(raw.get("via")),
        ssh_same_as_ftp=bool(raw.get("ssh_same_as_ftp", False)),
        ssh_host=str(raw.get("ssh_host") or ""),
        ssh_port=int(raw.get("ssh_port") or 22),
        ssh_user=str(raw.get("ssh_user") or ""),
        ssh_password=str(raw.get("ssh_password") or ""),
        ssh_password_env=str(raw.get("ssh_password_env") or ""),
        ssh_key_file=str(raw.get("ssh_key_file") or ""),
        ssh_key_passphrase_env=str(raw.get("ssh_key_passphrase_env") or ""),
        ssh_host_key_policy=str(raw.get("ssh_host_key_policy") or "tofu").lower(),
        helper_url=str(raw.get("helper_url") or ""),
        helper_token=str(raw.get("helper_token") or ""),
        helper_token_env=str(raw.get("helper_token_env") or ""),
        helper_path=str(raw.get("helper_path") or ""),
    )


def _via(value: Any) -> str:
    via = str(value or "direct").strip().lower()
    return via if via in {"direct", "ssh", "php"} else "direct"


# Settings the account form does not edit (they have their own page), so saving the form
# must carry them over from the existing entry with the same database id.
CONNECTION_FIELDS = (
    "via",
    "ssh_same_as_ftp",
    "ssh_host",
    "ssh_port",
    "ssh_user",
    "ssh_password",
    "ssh_password_env",
    "ssh_key_file",
    "ssh_key_passphrase_env",
    "ssh_host_key_policy",
    "helper_url",
    "helper_token",
    "helper_token_env",
    "helper_path",
)


def carry_connection_settings(old: DatabaseConfig | None, new: DatabaseConfig) -> DatabaseConfig:
    """Copy the connection method (direct / SSH tunnel / PHP helper) from `old` onto `new`."""
    if old is not None:
        for name in CONNECTION_FIELDS:
            setattr(new, name, getattr(old, name))
    return new


def _retention_from(raw: dict[str, Any] | None, global_mysql: int, files_forever: bool) -> RetentionConfig:
    raw = raw or {}
    return RetentionConfig(
        mysql_days=int(raw.get("mysql_days", global_mysql) or global_mysql),
        files_forever=bool(raw.get("files_forever", files_forever)),
        files_days=int(raw.get("files_days") or 0),
    )


def _notify_from(raw: dict[str, Any] | None) -> NotifyConfig:
    raw = raw or {}
    return NotifyConfig(
        webhook_url=str(raw.get("webhook_url") or ""),
        webhook_secret=str(raw.get("webhook_secret") or ""),
        smtp_host=str(raw.get("smtp_host") or ""),
        smtp_port=int(raw.get("smtp_port") or 587),
        smtp_user=str(raw.get("smtp_user") or ""),
        smtp_password=str(raw.get("smtp_password") or ""),
        smtp_password_env=str(raw.get("smtp_password_env") or ""),
        smtp_from=str(raw.get("smtp_from") or ""),
        smtp_to=str(raw.get("smtp_to") or ""),
        smtp_tls=bool(raw.get("smtp_tls", True)),
        phone_enabled=bool(raw.get("phone_enabled", False)),
        ntfy_server=str(raw.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
        ntfy_topic=str(raw.get("ntfy_topic") or ""),
        ntfy_details=bool(raw.get("ntfy_details", False)),
    )


def _account_from(raw: dict[str, Any], global_mysql: int, files_forever: bool) -> AccountConfig:
    aid = validate_account_id(
        str(raw.get("id") or "").strip() or _slug(str(raw.get("domain") or raw.get("label") or "site"))
    )
    # Drop legacy secrets-only accounts without ftp host — still load ftp empty
    ftp_raw = raw.get("ftp") if isinstance(raw.get("ftp"), dict) else {}
    dbs = [_db_from(d) for d in (raw.get("databases") or [])]
    return AccountConfig(
        id=aid,
        label=str(raw.get("label") or aid),
        domain=str(raw.get("domain") or aid),
        site_url=str(raw.get("site_url") or f"https://{raw.get('domain') or aid}"),
        enabled=bool(raw.get("enabled", True)),
        ftp=_ftp_from(ftp_raw),
        databases=dbs,
        retention=_retention_from(
            raw.get("retention") if isinstance(raw.get("retention"), dict) else None,
            global_mysql,
            files_forever,
        ),
    )


def default_config(root: Path | None = None) -> AppConfig:
    root = ensure_layout(root)
    return AppConfig(data_root=str(data_root(root)), accounts=[])


def load_config(path: Path | None = None) -> AppConfig:
    root = ensure_layout()
    file = path or accounts_file(root)
    if not file.is_file():
        cfg = default_config(root)
        save_config(cfg, file)
        return cfg
    raw = json.loads(file.read_text(encoding="utf-8"))
    version = int(raw.get("version") or 1)
    global_mysql = int(raw.get("mysql_retention_days", 15))
    files_forever = bool(raw.get("files_keep_forever", True))
    accounts = [
        _account_from(item, global_mysql, files_forever) for item in (raw.get("accounts") or [])
    ]
    cfg = AppConfig(
        version=max(version, CONFIG_VERSION),
        data_root=str(raw.get("data_root") or data_root(root)),
        mysql_retention_days=global_mysql,
        files_keep_forever=files_forever,
        large_table_mb=float(raw.get("large_table_mb", 50) or 50),
        download_retries=int(raw.get("download_retries", 3) or 3),
        compress_dumps=str(raw.get("compress_dumps") or "gzip"),
        ui_host=str(raw.get("ui_host") or "127.0.0.1"),
        ui_port=int(raw.get("ui_port") or 8787),
        ui_token=str(raw.get("ui_token") or os.environ.get("AVIDBACKUP_UI_TOKEN") or ""),
        notify=_notify_from(raw.get("notify") if isinstance(raw.get("notify"), dict) else None),
        accounts=accounts,
    )
    if version < CONFIG_VERSION:
        log.info("Migrated accounts.json schema v%s → v%s", version, CONFIG_VERSION)
        save_config(cfg, file)
    return cfg


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    root = ensure_layout()
    file = path or accounts_file(root)
    payload: dict[str, Any] = {
        "version": CONFIG_VERSION,
        "data_root": cfg.data_root or str(data_root(root)),
        "mysql_retention_days": cfg.mysql_retention_days,
        "files_keep_forever": cfg.files_keep_forever,
        "large_table_mb": cfg.large_table_mb,
        "download_retries": cfg.download_retries,
        "compress_dumps": cfg.compress_dumps,
        "ui_host": cfg.ui_host,
        "ui_port": cfg.ui_port,
        "ui_token": cfg.ui_token,
        "notify": asdict(cfg.notify),
        "accounts": [],
    }
    for a in cfg.accounts:
        validate_account_id(a.id)
        payload["accounts"].append(
            {
                "id": a.id,
                "label": a.label,
                "domain": a.domain,
                "site_url": a.site_url,
                "enabled": a.enabled,
                "ftp": asdict(a.ftp),
                "databases": [asdict(d) for d in a.databases],
                "retention": asdict(a.retention),
            }
        )
    tmp = file.with_suffix(file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(file)
    try:
        file.chmod(0o600)
    except OSError:
        pass
    if sys.platform.startswith("win"):
        log.debug(
            "accounts.json saved; on Windows POSIX mode 0600 is not enforced — "
            "restrict NTFS ACLs on %s",
            file,
        )
    return file


def upsert_account(cfg: AppConfig, account: AccountConfig) -> AppConfig:
    validate_account_id(account.id)
    out: list[AccountConfig] = []
    found = False
    for a in cfg.accounts:
        if a.id == account.id:
            out.append(account)
            found = True
        else:
            out.append(a)
    if not found:
        out.append(account)
    cfg.accounts = out
    return cfg


def delete_account(cfg: AppConfig, account_id: str) -> AppConfig:
    aid = validate_account_id(account_id)
    cfg.accounts = [a for a in cfg.accounts if a.id != aid]
    return cfg


def new_account_id(label_or_domain: str, existing: list[AccountConfig]) -> str:
    base = _slug(label_or_domain)
    validate_account_id(base)
    ids = {a.id for a in existing}
    if base not in ids:
        return base
    n = 2
    while f"{base}-{n}" in ids:
        n += 1
    return f"{base}-{n}"


def resolve_data_dir(cfg: AppConfig, account: AccountConfig) -> Path:
    validate_account_id(account.id)
    root = Path(cfg.data_root).expanduser() if cfg.data_root else data_root()
    return (root / account.id).resolve()
