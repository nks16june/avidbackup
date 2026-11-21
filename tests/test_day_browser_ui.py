from __future__ import annotations

from pathlib import Path

from avidbackup.day_browser import (
    list_directory,
    list_restore_days,
    resolve_download,
    snapshot_for_day,
)
from avidbackup.store import (
    AccountConfig,
    FtpConfig,
    load_config,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.webui.app import create_app


def _seed(account_id: str = "rp.site") -> Path:
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(id=account_id, label="RP", ftp=FtpConfig(host="h", user="u", password="p")),
    )
    save_config(cfg)
    cfg = load_config()
    data = resolve_data_dir(cfg, cfg.account(account_id))
    day = data / "2026-11-20"
    files = day / "files" / "public_html"
    files.mkdir(parents=True, exist_ok=True)
    (files / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    db = day / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "site.sql.gz").write_bytes(b"\x1f\x8b\x00")
    return data


def test_day_browser_helpers(backup_home):
    data = _seed("help.site")
    days = list_restore_days(data)
    assert [d.day for d in days] == ["2026-11-20"]
    snap = snapshot_for_day(data, "2026-11-20")
    assert snap is not None
    assert snap.has_files and snap.dumps
    rel, _abs, entries = list_directory(snap.files_dir, "")
    assert rel == ""
    assert any(e.name == "public_html" and e.is_dir for e in entries)
    rel2, _, entries2 = list_directory(snap.files_dir, "public_html")
    assert rel2 == "public_html"
    assert any(e.name == "index.html" and not e.is_dir for e in entries2)
    path = resolve_download(data, "2026-11-20", "files", "public_html/index.html")
    assert path.name == "index.html"
    dump = resolve_download(data, "2026-11-20", "db", "site.sql.gz")
    assert dump.name == "site.sql.gz"


def test_legacy_mysql_dumps_appear_on_day(backup_home):
    """Older runs wrote dumps to account/mysql/ — restore day must still list them."""
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(id="legacy.site", label="Legacy", ftp=FtpConfig(host="h", user="u", password="p")),
    )
    save_config(cfg)
    data = resolve_data_dir(load_config(), load_config().account("legacy.site"))
    mysql = data / "mysql"
    mysql.mkdir(parents=True)
    dump = mysql / "site_db_20261112_024654.sql"
    dump.write_text("-- dump", encoding="utf-8")

    snap = snapshot_for_day(data, "2026-11-12")
    assert snap is not None
    assert len(snap.dumps) == 1
    assert snap.dumps[0]["name"] == dump.name
    assert [d.day for d in list_restore_days(data)] == ["2026-11-12"]
    got = resolve_download(data, "2026-11-12", "db", dump.name)
    assert got == dump.resolve() or got.samefile(dump)

    client = create_app().test_client()
    page = client.get("/backups/legacy.site/2026-11-12?tab=db").get_data(as_text=True)
    assert dump.name in page
    assert "No database dumps" not in page
    dl = client.get(f"/backups/legacy.site/2026-11-12/download?kind=db&path={dump.name}")
    assert dl.status_code == 200
    assert b"-- dump" in dl.data


def test_restore_ui_routes(backup_home):
    _seed("ui.site")
    client = create_app().test_client()

    cal = client.get("/backups?account=ui.site").get_data(as_text=True)
    assert "Restore points" in cal
    assert "Browse / download" in cal or "Restore via FTP" in cal
    assert "/backups/ui.site/2026-11-20" in cal

    page = client.get("/backups/ui.site/2026-11-20").get_data(as_text=True)
    assert "Restore · 2026-11-20" in page or "2026-11-20" in page
    assert "public_html" in page
    assert "Databases" in page

    db_page = client.get("/backups/ui.site/2026-11-20?tab=db").get_data(as_text=True)
    assert "site.sql.gz" in db_page
    assert "Download" in db_page

    dl = client.get("/backups/ui.site/2026-11-20/download?kind=files&path=public_html/index.html")
    assert dl.status_code == 200
    assert b"<h1>ok</h1>" in dl.data

    bad = client.get("/backups/ui.site/2026-11-20/download?kind=files&path=../../secrets")
    assert bad.status_code == 404

    all_cal = client.get("/backups").get_data(as_text=True)
    assert "Open restore points" in all_cal or "restore points" in all_cal.lower()
