"""Regression tests for the UI security hardening (host check, sign-in, restore guardrails)."""

from __future__ import annotations

import json
import re
import time

import pytest

from avidbackup.audit import write_audit
from avidbackup.paths import audit_log_path, ui_token_path
from avidbackup.store import (
    AccountConfig,
    FtpConfig,
    load_config,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.webui.app import create_app
from avidbackup.webui.security import (
    LoginLimiter,
    host_allowed,
    hostname_of,
    load_or_create_ui_token,
    safe_next,
    token_fingerprint,
)


@pytest.fixture
def auth_on(backup_home, monkeypatch):
    monkeypatch.delenv("AVIDBACKUP_UI_AUTH", raising=False)
    return backup_home


def _csrf(client) -> str:
    client.get("/login")
    with client.session_transaction() as sess:
        sess.setdefault("csrf_token", "x" * 64)
        return sess["csrf_token"]


def _sign_in(client, token: str):
    return client.post("/login", data={"token": token, "csrf_token": _csrf(client), "next": "/"})


# ---- Host header (DNS rebinding) ------------------------------------------------------


def test_hostname_parsing():
    assert hostname_of("localhost:8787") == "localhost"
    assert hostname_of("127.0.0.1") == "127.0.0.1"
    assert hostname_of("[::1]:8787") == "::1"
    assert hostname_of("Evil.Example.NET:80") == "evil.example.net"
    assert hostname_of("") == ""


@pytest.mark.parametrize("host", ["localhost", "localhost:8787", "127.0.0.1:54824", "[::1]:8787"])
def test_loopback_hosts_allowed(host):
    assert host_allowed(host)


@pytest.mark.parametrize(
    "host",
    ["evil.example.net", "evil.example.net:8787", "127.0.0.1.evil.net", "localhost.evil.net", "", "192.168.1.5"],
)
def test_foreign_hosts_rejected(host):
    assert not host_allowed(host)


def test_rebinding_request_is_rejected(backup_home):
    client = create_app().test_client()
    assert client.get("/", headers={"Host": "attacker.example.net:8787"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:8787"}).status_code == 200
    # rejected before CSRF/auth, so a rebinding POST cannot even reach the action
    resp = client.post("/run-all", headers={"Host": "attacker.example.net"}, data={})
    assert resp.status_code == 400


def test_extra_allowed_host_from_env(backup_home, monkeypatch):
    monkeypatch.setenv("AVIDBACKUP_ALLOWED_HOSTS", "nas.lan, backup.lan")
    client = create_app().test_client()
    assert client.get("/", headers={"Host": "nas.lan:8787"}).status_code == 200
    assert client.get("/", headers={"Host": "other.lan"}).status_code == 400


# ---- Sign-in -------------------------------------------------------------------------


def test_token_is_generated_on_first_run_and_required(auth_on):
    app = create_app()
    assert ui_token_path().is_file()
    token = ui_token_path().read_text(encoding="utf-8").strip()
    assert len(token) >= 24
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/backups").status_code == 302
    assert client.get("/accounts/new").status_code == 302
    assert _sign_in(client, "wrong-token").status_code == 401
    ok = _sign_in(client, token)
    assert ok.status_code == 302
    assert client.get("/").status_code == 200


def test_auth_can_be_switched_off_explicitly(backup_home):
    client = create_app().test_client()
    assert client.get("/").status_code == 200  # fixture sets AVIDBACKUP_UI_AUTH=off


def test_explicit_env_token_wins(auth_on, monkeypatch):
    monkeypatch.setenv("AVIDBACKUP_UI_TOKEN", "my-own-token-123")
    client = create_app().test_client()
    assert _sign_in(client, "my-own-token-123").status_code == 302
    assert client.get("/").status_code == 200


def test_rotating_the_token_signs_everyone_out(auth_on):
    app_a = create_app()
    client_a = app_a.test_client()
    token_a = ui_token_path().read_text(encoding="utf-8").strip()
    _sign_in(client_a, token_a)
    with client_a.session_transaction() as sess:
        saved = dict(sess)
    assert saved["ui_tok"] == token_fingerprint(token_a)

    new_token = load_or_create_ui_token(rotate=True)
    assert new_token != token_a
    client_b = create_app().test_client()
    with client_b.session_transaction() as sess:
        sess.update(saved)  # same browser cookie, old token
    assert client_b.get("/").status_code == 302


def test_login_lockout_after_repeated_failures(auth_on):
    app = create_app()
    client = app.test_client()
    token = ui_token_path().read_text(encoding="utf-8").strip()
    for _ in range(5):
        assert _sign_in(client, "nope").status_code == 401
    # even the right token is refused while locked out
    assert _sign_in(client, token).status_code == 429


def test_limiter_window_expires():
    lim = LoginLimiter(max_failures=2, window=10)
    lim.record_failure("a", now=100)
    lim.record_failure("a", now=101)
    assert lim.blocked("a", now=102)
    assert not lim.blocked("a", now=200)
    assert not lim.blocked("someone-else", now=102)


# ---- Open redirect -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["//evil.test", "http://evil.test/x", "https://evil.test", "/\\evil.test", "javascript:alert(1)", "evil", "/ok\n/x", "", None],
)
def test_safe_next_rejects_external_targets(value):
    assert safe_next(value, "/home") == "/home"


@pytest.mark.parametrize("value", ["/", "/backups", "/backups?days=7", "/accounts/a.b/edit"])
def test_safe_next_keeps_local_paths(value):
    assert safe_next(value, "/home") == value


def test_login_never_redirects_off_site(auth_on):
    client = create_app().test_client()
    token = ui_token_path().read_text(encoding="utf-8").strip()
    resp = client.post(
        "/login",
        data={"token": token, "csrf_token": _csrf(client), "next": "//evil.example.net/phish"},
    )
    assert resp.status_code == 302
    assert "evil.example.net" not in resp.headers["Location"]


# ---- Cookies, headers, Origin ---------------------------------------------------------


def test_session_cookie_is_strict_httponly_and_named(backup_home):
    client = create_app().test_client()
    cookies = client.get("/").headers.getlist("Set-Cookie")
    cookie = next(c for c in cookies if c.startswith("avidbackup_session="))
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_headers_have_no_external_hosts(backup_home):
    resp = create_app().test_client().get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "googleapis" not in csp and "gstatic" not in csp
    assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["X-Frame-Options"] == "DENY"
    # Must stay same-origin: no-referrer makes browsers send "Origin: null" on our own forms.
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert resp.headers["Cache-Control"]


def test_pages_load_nothing_from_the_internet(backup_home):
    client = create_app().test_client()
    for path in ("/", "/backups", "/settings", "/accounts/new"):
        html = client.get(path).get_data(as_text=True)
        assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html, path
        external = re.findall(r"""(?:src|href)=["'](https?://[^"']+)""", html)
        assert not [u for u in external if "localhost" not in u and "127.0.0.1" not in u], (path, external)


def test_bundled_fonts_are_served_locally(backup_home):
    client = create_app().test_client()
    resp = client.get("/static/fonts/source-sans-3-latin-400-normal.woff2")
    assert resp.status_code == 200
    assert resp.data[:4] == b"wOF2"
    assert client.get("/static/fonts/ibm-plex-mono-latin-400-normal.woff2").status_code == 200
    assert "fonts/source-sans-3-latin-400-normal.woff2" in client.get("/").get_data(as_text=True)


def test_cross_origin_post_is_blocked_even_with_valid_csrf(backup_home):
    client = create_app().test_client()
    token = _csrf(client)
    bad = client.post(
        "/run-all", data={"csrf_token": token}, headers={"Origin": "http://attacker.example.net"}
    )
    assert bad.status_code == 400
    null_origin = client.post("/run-all", data={"csrf_token": token}, headers={"Origin": "null"})
    assert null_origin.status_code == 400


# ---- FTP restore guardrails ------------------------------------------------------------


@pytest.fixture
def restore_site(backup_home):
    cfg = load_config()
    upsert_account(
        cfg,
        AccountConfig(id="rs.site", label="RS", ftp=FtpConfig(host="ftp.rs.test", user="u", password="s3cr3t-ftp-pw")),
    )
    save_config(cfg)
    cfg = load_config()
    data = resolve_data_dir(cfg, cfg.account("rs.site"))
    files = data / "2026-11-20" / "files" / "public_html"
    files.mkdir(parents=True)
    (files / "index.php").write_text("<?php echo 1;", encoding="utf-8")
    (files / "app.css").write_text("body{}", encoding="utf-8")
    return data


@pytest.fixture
def uploads(monkeypatch):
    calls: list[dict] = []

    def fake_restore(settings, files_dir, *, rel="", client=None, max_files=0):
        from avidbackup.restore_ftp import FtpRestoreStats

        calls.append({"rel": rel})
        return FtpRestoreStats(uploaded=2, bytes=20)

    monkeypatch.setattr("avidbackup.restore_ftp.restore_day_files_via_ftp", fake_restore)
    return calls


PREVIEW = "/backups/rs.site/2026-11-20/restore-ftp/preview"
RESTORE = "/backups/rs.site/2026-11-20/restore-ftp"


def _preview(client, rel=""):
    token = _csrf(client)
    resp = client.post(PREVIEW, data={"csrf_token": token, "path": rel})
    with client.session_transaction() as sess:
        plan = sess.get("restore_plan") or {}
    return token, resp, plan


def test_restore_without_preview_is_refused(restore_site, uploads):
    client = create_app().test_client()
    token = _csrf(client)
    resp = client.post(RESTORE, data={"csrf_token": token, "confirm": "rs.site", "plan_id": "x"})
    assert resp.status_code == 302
    assert uploads == []


def test_old_style_confirm_word_no_longer_works(restore_site, uploads):
    client = create_app().test_client()
    token, _resp, plan = _preview(client)
    client.post(RESTORE, data={"csrf_token": token, "confirm": "restore", "plan_id": plan["id"]})
    assert uploads == []


def test_preview_shows_scope_and_sends_nothing(restore_site, uploads):
    client = create_app().test_client()
    _token, resp, plan = _preview(client)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Confirm restore to your server" in html
    assert "2 file(s)" in html and "ftp.rs.test" in html
    assert "overwritten" in html
    assert plan["files"] == 2 and plan["account"] == "rs.site" and plan["day"] == "2026-11-20"
    assert uploads == []


def test_restore_needs_exact_site_id(restore_site, uploads):
    client = create_app().test_client()
    token, _resp, plan = _preview(client)
    wrong = client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "RS.SITE "})
    assert wrong.status_code == 302 and uploads == []
    wrong2 = client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "restore"})
    assert uploads == []
    ok = client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site"})
    assert ok.status_code == 302 and len(uploads) == 1
    assert wrong2.status_code == 302


def test_preview_ticket_is_single_use(restore_site, uploads):
    client = create_app().test_client()
    token, _resp, plan = _preview(client)
    data = {"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site"}
    client.post(RESTORE, data=data)
    client.post(RESTORE, data=data)  # replay
    assert len(uploads) == 1


def test_preview_ticket_expires(restore_site, uploads, monkeypatch):
    client = create_app().test_client()
    token, _resp, plan = _preview(client)
    real = time.time
    monkeypatch.setattr("avidbackup.webui.app.time.time", lambda: real() + 3600)
    client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site"})
    assert uploads == []


def test_ticket_is_bound_to_the_previewed_folder(restore_site, uploads):
    client = create_app().test_client()
    token, _resp, plan = _preview(client, rel="public_html/app.css")
    # confirming a different scope than previewed is refused and burns the ticket
    client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site", "path": ""})
    assert uploads == []
    same = {"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site", "path": "public_html/app.css"}
    client.post(RESTORE, data=same)
    assert uploads == []  # the burned ticket cannot be reused
    # a fresh preview of the intended scope works
    token, _resp, plan = _preview(client, rel="public_html/app.css")
    client.post(RESTORE, data={**same, "plan_id": plan["id"], "csrf_token": token})
    assert uploads == [{"rel": "public_html/app.css"}]


def test_restore_steps_are_audited(restore_site, uploads):
    client = create_app().test_client()
    token, _resp, plan = _preview(client)
    client.post(RESTORE, data={"csrf_token": token, "plan_id": plan["id"], "confirm": "rs.site"})
    lines = audit_log_path().read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line)["action"] for line in lines]
    assert actions == ["restore_ftp_previewed", "restore_ftp_started", "restore_ftp_finished"]
    assert "s3cr3t-ftp-pw" not in "\n".join(lines)


def test_audit_entries_are_json_lines_and_skip_empty_fields(backup_home):
    write_audit("probe", account="a", detail=None)
    entry = json.loads(audit_log_path().read_text(encoding="utf-8").splitlines()[-1])
    assert entry["action"] == "probe" and entry["account"] == "a"
    assert "detail" not in entry and entry["time"].endswith("Z")
