from __future__ import annotations

from avidbackup.store import AccountConfig, FtpConfig, load_config, save_config, upsert_account
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
