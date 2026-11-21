from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from avidbackup.backup_report import build_report, human_bytes
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
UTC = timezone.utc


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
    db = data_dir / "history.sqlite3"
    HistoryStore(db)  # creates schema
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


def _add_dump(data_dir: Path, name: str, when: datetime, size: int = 100) -> Path:
    mysql = data_dir / "mysql"
    mysql.mkdir(parents=True, exist_ok=True)
    p = mysql / name
    p.write_bytes(b"x" * size)
    (mysql / f"{name}.sha256").write_text("abc")
    ts = when.timestamp()
    os.utime(p, (ts, ts))
    return p


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_day_wise_statuses_and_missed_days(backup_home):
    cfg, data = _account(backup_home)
    day = lambda n: NOW - timedelta(days=n)  # noqa: E731
    _add_run(data, day(4), "success", files_downloaded=5, files_bytes=500, mysql_downloaded=1, mysql_bytes=100)
    _add_run(data, day(3), "failed", error="boom")
    # day(2): nothing ran -> missed
    _add_run(data, day(1), "failed", error="first try")
    _add_run(data, day(1) + timedelta(hours=1), "success", files_downloaded=2)
    _add_run(data, day(0) - timedelta(hours=1), "success", files_downloaded=1)

    rep = build_report(cfg, days=7, now=NOW, tz=UTC)
    cells = rep.accounts[0].cells
    d = lambda n: (NOW - timedelta(days=n)).date()  # noqa: E731
    assert cells[d(6)] == "na" and cells[d(5)] == "na"  # before the first backup
    assert cells[d(4)] == "ok"
    assert cells[d(3)] == "fail"
    assert cells[d(2)] == "missed"
    assert cells[d(1)] == "ok"  # failed, then succeeded later: last run wins
    assert cells[d(0)] == "ok"
    assert rep.failed_runs == 2
    assert rep.expected_days == 5 and rep.covered_days == 3
    assert rep.coverage_pct == 60
    assert rep.last_success is not None and rep.last_success.date() == d(0)
    # newest day first
    assert rep.day_rows[0].day == d(0)
    assert rep.day_rows[4].runs and rep.day_rows[4].new_files == 5


def test_failed_after_success_is_warning(backup_home):
    cfg, data = _account(backup_home)
    _add_run(data, NOW - timedelta(hours=3), "success")
    _add_run(data, NOW - timedelta(hours=1), "failed")
    rep = build_report(cfg, days=7, now=NOW, tz=UTC)
    assert rep.accounts[0].cells[NOW.date()] == "warn"


def test_stale_running_run_counts_as_failed(backup_home):
    cfg, data = _account(backup_home)
    _add_run(data, NOW - timedelta(hours=30), "running", finished_at=None)
    rep = build_report(cfg, days=7, now=NOW, tz=UTC)
    row = [r for r in rep.day_rows if r.runs][0].runs[0]
    assert row.status == "interrupted"
    assert rep.accounts[0].cells[(NOW - timedelta(hours=30)).date()] == "fail"


def test_dumps_grouped_by_day_and_manifest_totals(backup_home):
    cfg, data = _account(backup_home)
    _add_run(data, NOW - timedelta(days=1), "success")
    _add_dump(data, "db_20261119.sql.gz", NOW - timedelta(days=1), size=300)
    _add_dump(data, "db_20261119.sqlite", NOW - timedelta(days=1), size=200)
    (data / "mysql" / ".sqlite_fetch_db1").write_bytes(b"leftover")  # ignored
    (data / ".manifest.json").write_text(
        json.dumps({"files": {"/a": {"size": 1000}, "/b": {"size": 2000}}})
    )
    rep = build_report(cfg, days=7, now=NOW, tz=UTC)
    yesterday = [r for r in rep.day_rows if r.day == (NOW - timedelta(days=1)).date()][0]
    assert {d.name for d in yesterday.dumps} == {"db_20261119.sql.gz", "db_20261119.sqlite"}
    assert all(d.checksum for d in yesterday.dumps)
    acct = rep.accounts[0]
    assert (acct.files_tracked, acct.files_bytes) == (2, 3000)
    assert acct.dumps_count == 2 and acct.dumps_bytes == 500
    assert rep.total_bytes == 3500


def test_report_never_creates_or_changes_history(backup_home):
    cfg, data = _account(backup_home)
    build_report(cfg, days=7, now=NOW, tz=UTC)
    assert not (data / "history.sqlite3").exists()


def test_account_filter(backup_home):
    cfg, data = _account(backup_home, "one.test")
    cfg2, _ = _account(backup_home, "two.test")
    rep = build_report(cfg2, days=7, account_id="two.test", now=NOW, tz=UTC)
    assert [a.account_id for a in rep.accounts] == ["two.test"]


def test_backups_page_renders_and_validates(backup_home):
    cfg, data = _account(backup_home)
    _add_run(data, datetime.now(timezone.utc) - timedelta(hours=2), "success", files_downloaded=3)
    client = create_app().test_client()
    resp = client.get("/backups?days=30")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Backup calendar" in html and "Coverage" in html
    assert client.get("/backups?days=999").status_code == 200  # falls back to 30
    bad = client.get("/backups?account=../../etc")
    assert bad.status_code == 302  # rejected, redirected
    assert client.get("/backups?account=nope.test").status_code == 302


def test_day_status_names_the_affected_account(backup_home):
    cfg, data_a = _account(backup_home, "a.test")
    cfg, data_b = _account(backup_home, "b.test")  # same label "Site" on purpose
    _add_run(data_a, NOW - timedelta(hours=2), "success")
    _add_run(data_b, NOW - timedelta(days=1), "success")  # b has history but no run today
    rep = build_report(cfg, days=3, now=NOW, tz=UTC)
    today = rep.day_rows[0]
    assert today.status == "missed"
    assert today.account_status == {"a.test": "ok", "b.test": "missed"}
    assert today.accounts_with("missed") == ["Site"]
    assert today.accounts_with("ok") == ["Site"]
