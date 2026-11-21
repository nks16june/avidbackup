"""Completion honesty: partial status, discover secrets, stop route."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from avidbackup.backup_report import build_report
from avidbackup.history import HistoryStore
from avidbackup.store import (
    AccountConfig,
    FtpConfig,
    load_config,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.webui.app import create_app

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _account(home: Path, account_id: str = "site.test"):
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(id=account_id, label="Site", ftp=FtpConfig(host="h", user="u", password="p")),
    )
    save_config(cfg)
    cfg = load_config()
    return cfg, resolve_data_dir(cfg, cfg.account(account_id))


def _add_run(data_dir: Path, started: datetime, status: str, **cols) -> None:
    import sqlite3

    db = data_dir / "history.sqlite3"
    HistoryStore(db)
    fields = {
        "started_at": _iso(started),
        "finished_at": _iso(started + timedelta(seconds=30)),
        "status": status,
        **cols,
    }
    conn = sqlite3.connect(db)
    conn.execute(
        f"INSERT INTO runs ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
        list(fields.values()),
    )
    conn.commit()
    conn.close()


def test_partial_run_is_warn_day_files_ok_db_fail(backup_home):
    cfg, data = _account(backup_home)
    _add_run(
        data,
        NOW - timedelta(hours=1),
        "partial",
        files_downloaded=10,
        files_failed=0,
        mysql_downloaded=0,
        error="Files OK; database dump failed (Access denied)",
    )
    rep = build_report(cfg, days=7, now=NOW, tz=timezone.utc)
    acct = rep.accounts[0]
    today = NOW.date()
    assert acct.cells[today] == "warn"
    assert acct.files_cells[today] == "ok"
    assert acct.db_cells[today] == "fail"


def test_discover_json_omits_password(backup_home):
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(
            id="sec.site",
            label="Sec",
            ftp=FtpConfig(host="h", user="u", password="ftp-secret"),
        ),
    )
    save_config(cfg)

    fake_creds = MagicMock()
    fake_creds.port = 3306
    fake_creds.user = "dbuser"
    fake_creds.password = "DB-PASSWORD-SECRET"
    fake_creds.database = "wpdb"
    fake_creds.source_path = "/public_html/wp-config.php"

    client = create_app().test_client()
    client.get("/")  # session + csrf
    # csrf token is rendered into the form page
    edit = client.get("/accounts/sec.site/edit")
    html = edit.get_data(as_text=True)
    # extract csrf from hidden input
    import re

    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "csrf token missing"
    csrf = m.group(1)

    with (
        patch("avidbackup.remote.connect") as connect_mock,
        patch(
            "avidbackup.mysql_config.discover_all_mysql_credentials",
            return_value=[fake_creds],
        ),
        patch("avidbackup.mysql_config.resolve_direct_host", return_value="h"),
    ):
        connect_mock.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect_mock.return_value.__exit__ = MagicMock(return_value=False)
        resp = client.post(
            "/accounts/discover-db",
            data={
                "csrf_token": csrf,
                "id": "sec.site",
                "ftp_protocol": "ftps",
                "ftp_host": "h",
                "ftp_port": "21",
                "ftp_user": "u",
                "ftp_password": "ftp-secret",
                "ftp_remote_root": "/",
            },
            headers={
                "X-Requested-With": "fetch",
                "Accept": "application/json",
                "X-CSRF-Token": csrf,
            },
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["found"]
    assert "password" not in body["found"][0]
    assert body["found"][0]["password_set"] is True
    assert "DB-PASSWORD-SECRET" not in resp.get_data(as_text=True)


def test_stop_backup_route_clears_stale(backup_home):
    cfg, data = _account(backup_home, "stop.site")
    data.mkdir(parents=True, exist_ok=True)
    (data / ".backup_run.lock").write_text("999999\n", encoding="utf-8")
    client = create_app().test_client()
    edit = client.get("/")
    html = edit.get_data(as_text=True)
    import re

    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    csrf = m.group(1) if m else ""
    resp = client.post(
        "/accounts/stop.site/stop",
        data={"csrf_token": csrf, "next": "index"},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert not (data / ".backup_run.lock").exists() or "stale" in resp.get_data(as_text=True).lower()
