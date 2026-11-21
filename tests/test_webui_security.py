from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from avidbackup.daystore import day_stamp
from avidbackup.history import HistoryStore
from avidbackup.initial_full import MARKER_NAME
from avidbackup.store import (
    AccountConfig,
    FtpConfig,
    load_config,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.webui.app import create_app
from avidbackup.webui.security import is_loopback_host


def test_loopback_detection():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.1")


def test_non_loopback_refused_without_token(backup_home, monkeypatch):
    from avidbackup.webui.app import run_ui

    monkeypatch.delenv("AVIDBACKUP_UI_TOKEN", raising=False)
    cfg = load_config()
    cfg.ui_token = ""
    save_config(cfg)
    assert run_ui(host="0.0.0.0", port=18787) == 2


def test_csrf_rejects_post(backup_home):
    app = create_app()
    client = app.test_client()
    # GET to establish session/csrf
    client.get("/")
    resp = client.post("/run-all", data={})
    assert resp.status_code == 400


def test_passwords_not_echoed(backup_home):
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(
            id="ex.com",
            label="Ex",
            ftp=FtpConfig(host="h", user="u", password="SUPERSECRET"),
        ),
    )
    save_config(cfg)
    app = create_app()
    client = app.test_client()
    # need CSRF for nothing on GET
    html = client.get("/accounts/ex.com/edit").get_data(as_text=True)
    assert "SUPERSECRET" not in html
    assert "leave blank to keep" in html.lower() or "••••" in html


def _seed_account(account_id: str = "site.test") -> Path:
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(id=account_id, label="Site", ftp=FtpConfig(host="h", user="u", password="p")),
    )
    save_config(cfg)
    cfg = load_config()
    data = resolve_data_dir(cfg, cfg.account(account_id))
    data.mkdir(parents=True, exist_ok=True)
    return data


def test_index_shows_graphical_today_status(backup_home):
    data = _seed_account("ok.site")
    (data / MARKER_NAME).write_text("done", encoding="utf-8")
    HistoryStore(data / "history.sqlite3")
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    import sqlite3

    conn = sqlite3.connect(data / "history.sqlite3")
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, status, files_downloaded) VALUES (?, ?, ?, ?)",
        (
            started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            (started + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "success",
            2,
        ),
    )
    conn.commit()
    conn.close()

    html = create_app().test_client().get("/").get_data(as_text=True)
    assert 'class="pill ok"' in html
    assert "Protected" in html
    assert "Start" in html or "Resume" in html
    assert "Folder" in html
    assert "/accounts/ok.site/open-folder" in html
    assert "site-row" in html
    assert "Backup fleet" in html or "Protected" in html


def test_index_needs_full_badge_when_no_initial(backup_home):
    _seed_account("new.site")
    html = create_app().test_client().get("/").get_data(as_text=True)
    assert 'class="pill needs_full"' in html
    assert "Needs full" in html


def test_account_log_json_tails_file(backup_home, monkeypatch):
    from avidbackup.paths import logs_dir

    _seed_account("log.site")
    folder = logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "backup_20261121_log.site.log"
    path.write_text(
        "2026-11-21 12:00:00 [INFO] [log.site] hello\n"
        "2026-11-21 12:00:01 [ERROR] [log.site] boom\n",
        encoding="utf-8",
    )
    client = create_app().test_client()
    page = client.get("/accounts/log.site").get_data(as_text=True)
    assert "Live log" in page
    assert "/accounts/log.site/log.json" in page
    data = client.get("/accounts/log.site/log.json").get_json()
    assert "hello" in data["text"]
    assert data["name"] == path.name
    more = client.get(f"/accounts/log.site/log.json?offset={data['offset']}").get_json()
    assert more["text"] == ""


def test_account_live_console_and_json(backup_home):
    import json
    import time

    data = _seed_account("live.site")
    (data / ".transfer_status.json").write_text(
        json.dumps(
            {
                "account_id": "live.site",
                "phase": "transferring",
                "message": "Downloading /public_html/a.css",
                "started_at": time.time() - 30,
                "updated_at": time.time(),
                "scanned": 100,
                "queued": 40,
                "downloaded": 12,
                "failed": 0,
                "skipped": 60,
                "bytes_done": 1024,
                "bytes_total": 4096,
                "current_path": "/public_html/a.css",
                "current_size": 512,
                "current_transferred": 128,
                "speed_bps": 2048,
                "eta_seconds": 10,
                "recent": [{"path": "/public_html/a.css", "size": 512, "status": "transferring"}],
                "samples": [{"t": time.time(), "bps": 2048, "bytes": 1024}],
            }
        ),
        encoding="utf-8",
    )
    # Hold a live lock so the UI marks the transfer as active.
    (data / ".backup_run.lock").write_text(f"{__import__('os').getpid()}\nlive.site\n", encoding="utf-8")

    client = create_app().test_client()
    html = client.get("/accounts/live.site").get_data(as_text=True)
    assert "Live transfer console" in html
    assert "Throughput" in html
    assert "File activity" in html
    assert 'id="ring-prog"' in html

    payload = client.get("/accounts/live.site/live.json").get_json()
    assert payload["live"] is True
    assert payload["phase"] == "transferring"
    assert payload["downloaded"] == 12
    assert payload["pct"] > 0
    assert payload["current_path"].endswith("a.css")

    index = client.get("/").get_data(as_text=True)
    assert "/accounts/live.site" in index
    assert ">Live<" in index or 'href="/accounts/live.site"' in index


def test_open_folder_opens_today_dir(backup_home, monkeypatch):
    data = _seed_account("fold.site")
    today = data / day_stamp()
    today.mkdir(parents=True, exist_ok=True)
    opened: list[Path] = []

    def _fake_open(path: Path) -> None:
        opened.append(Path(path))

    monkeypatch.setattr("avidbackup.webui.app._open_in_file_manager", _fake_open)
    client = create_app().test_client()
    client.get("/")
    with client.session_transaction() as sess:
        tok = sess["csrf_token"]
    resp = client.post(
        "/accounts/fold.site/open-folder",
        data={"csrf_token": tok},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert opened and opened[0].resolve() == today.resolve()


def test_open_folder_rejects_unknown_and_traversal(backup_home):
    client = create_app().test_client()
    client.get("/")
    with client.session_transaction() as sess:
        tok = sess["csrf_token"]
    assert client.post(
        "/accounts/nope.site/open-folder",
        data={"csrf_token": tok},
        follow_redirects=True,
    ).status_code == 200
    assert client.post(
        "/accounts/%2e%2e/open-folder",
        data={"csrf_token": tok},
        follow_redirects=True,
    ).status_code == 200
