"""Set up, test and remove the ways AvidBackup reaches a database (SSH tunnel, PHP helper).

Shared by the command line (`avidbackup helper ...`, `avidbackup db-test`) and the web page.
These functions change the in-memory config; the caller saves it.
"""

from __future__ import annotations

import logging
from typing import Any

from avidbackup.databases.registry import get_dumper
from avidbackup.php_helper import HelperError, HelperInfo, install_helper, new_token, remove_helper
from avidbackup.remote import connect
from avidbackup.secure_cli import resolve_secret
from avidbackup.settings_builder import settings_from_account

log = logging.getLogger(__name__)


def _require_mysql(db: Any) -> None:
    if str(getattr(db, "type", "mysql")).lower() not in {"mysql", "mariadb"}:
        raise HelperError("The PHP helper only supports MySQL / MariaDB databases.")


def install_helper_for(cfg, account, db, *, base_url: str = "", allowed_ips: str = "") -> HelperInfo:  # noqa: ANN001
    """Upload a fresh helper for `db` and switch the database to via=php."""
    _require_mysql(db)
    settings = settings_from_account(cfg, account)
    password = resolve_secret(db.password, getattr(db, "password_env", "") or "")
    token = new_token()
    with connect(settings) as remote:
        if db.helper_path:  # replace an earlier helper instead of leaving it behind
            try:
                remote.delete_file(db.helper_path)
            except Exception as exc:  # noqa: BLE001
                log.info("Previous helper %s could not be removed: %s", db.helper_path, exc)
        info = install_helper(
            remote,
            web_root=settings.remote_files_root,
            site_url=settings.site_url,
            token=token,
            db_host=db.host or "localhost",
            db_port=int(db.port or 3306),
            db_user=db.user,
            db_password=password,
            db_name=db.database,
            url=base_url,
            allowed_ips=allowed_ips,
        )
    db.via = "php"
    db.helper_url = info.url
    db.helper_path = info.path
    db.helper_token = info.token
    db.helper_token_env = ""
    return info


def remove_helper_for(cfg, account, db) -> None:  # noqa: ANN001
    """Delete the helper from the server and switch the database back to a direct connection."""
    settings = settings_from_account(cfg, account)
    with connect(settings) as remote:
        remove_helper(remote, db.helper_path)
    db.via = "direct"
    db.helper_url = ""
    db.helper_path = ""
    db.helper_token = ""
    db.helper_token_env = ""


def test_database_connection(cfg, account, db) -> str:  # noqa: ANN001
    """Try the configured connection method. Returns a one-line description; raises on failure."""
    settings = settings_from_account(cfg, account)
    if str(db.type).lower() == "sqlite":
        raise HelperError("SQLite databases are fetched with the site files; there is no connection to test.")
    dumper = get_dumper(db, settings)
    dumper.test_connection()
    via = {"direct": "directly", "ssh": "through the SSH tunnel", "php": "through the PHP helper"}.get(
        db.via, db.via
    )
    return f"Connected {via} to {db.database or db.id}."
