from __future__ import annotations

import pytest

from avidbackup import db_connection
from avidbackup.store import (
    AccountConfig,
    DatabaseConfig,
    FtpConfig,
    load_config,
    save_config,
    upsert_account,
)
from avidbackup.webui.app import create_app


def _add(protocol: str = "ftps", **db_over) -> None:  # noqa: ANN003
    cfg = load_config()
    db = DatabaseConfig(id="main", type="mysql", host="localhost", user="dbu", password="dbpw", database="shop", **db_over)
    upsert_account(
        cfg,
        AccountConfig(id="w.test", label="Web Test", site_url="https://w.test",
                      ftp=FtpConfig(protocol=protocol, host="ftp.w.test", user="u", password="p"), databases=[db]),
    )
    save_config(cfg)


def _db():  # noqa: ANN202
    return load_config().account("w.test").databases[0]


def _client():  # noqa: ANN202
    client = create_app().test_client()
    client.get("/")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    return client, token


PAGE = "/accounts/w.test/db/main/connection"


def test_page_renders_and_unknown_targets_are_404(backup_home):
    _add()
    client, _ = _client()
    html = client.get(PAGE).get_data(as_text=True)
    assert "Connection method" in html and "SSH tunnel" in html and "PHP helper" in html
    assert client.get("/accounts/nope/db/main/connection").status_code == 404
    assert client.get("/accounts/w.test/db/nope/connection").status_code == 404
    assert client.get("/accounts/..%2f..%2fx/db/main/connection").status_code == 404


def test_page_never_shows_saved_secrets(backup_home):
    _add(via="ssh", ssh_host="ssh.w.test", ssh_user="me", ssh_password="ssh-secret-pw",
         helper_url="https://w.test/avidbackup-abc.php", helper_token="helper-secret-token-" + "x" * 20)
    client, _ = _client()
    html = client.get(PAGE).get_data(as_text=True)
    assert "ssh-secret-pw" not in html and "helper-secret-token" not in html and "dbpw" not in html
    assert "one is saved" in html and "secret token saved" in html


def test_post_needs_the_csrf_token(backup_home):
    _add()
    client = create_app().test_client()
    assert client.post(PAGE, data={"via": "ssh"}).status_code == 400


def test_save_ssh_tunnel_settings(backup_home):
    _add()
    client, token = _client()
    resp = client.post(PAGE, data={"csrf_token": token, "via": "ssh", "ssh_host": "ssh.w.test", "ssh_port": "2222",
                                   "ssh_user": "me", "ssh_password": "first-pw", "db_host": "127.0.0.1",
                                   "db_port": "3306", "ssh_host_key_policy": "strict"})
    assert resp.status_code == 302
    db = _db()
    assert (db.via, db.ssh_host, db.ssh_port, db.ssh_user, db.ssh_password) == ("ssh", "ssh.w.test", 2222, "me", "first-pw")
    assert db.ssh_host_key_policy == "strict" and db.host == "127.0.0.1"
    # saving again with a blank password keeps the saved one
    client.post(PAGE, data={"csrf_token": token, "via": "ssh", "ssh_host": "ssh.w.test", "ssh_user": "me2",
                            "ssh_password": "", "db_host": "127.0.0.1"})
    again = _db()
    assert again.ssh_password == "first-pw" and again.ssh_user == "me2"


@pytest.mark.parametrize(
    "data, message",
    [
        ({"via": "ssh"}, "SSH host and user"),
        ({"via": "ssh", "ssh_host": "h", "ssh_user": "u", "ssh_port": "70000"}, "port"),
        ({"via": "ssh", "ssh_host": "h", "ssh_user": "u", "ssh_port": "abc"}, "port"),
        ({"via": "ssh", "ssh_same_as_ftp": "on"}, "SFTP"),
        ({"via": "php"}, "Install the PHP helper first"),
    ],
)
def test_invalid_choices_are_refused_and_not_saved(backup_home, data, message):
    _add()
    client, token = _client()
    client.post(PAGE, data={"csrf_token": token, **data})
    assert _db().via == "direct"
    with client.session_transaction() as sess:
        flashed = " ".join(msg for _cat, msg in sess.get("_flashes", []))
    assert message in flashed


def test_reusing_the_sftp_login_is_allowed_for_sftp_sites(backup_home):
    _add(protocol="sftp")
    client, token = _client()
    client.post(PAGE, data={"csrf_token": token, "via": "ssh", "ssh_same_as_ftp": "on", "db_host": "127.0.0.1"})
    db = _db()
    assert db.via == "ssh" and db.ssh_same_as_ftp is True


def test_unknown_method_is_a_bad_request(backup_home):
    _add()
    client, token = _client()
    assert client.post(PAGE, data={"csrf_token": token, "via": "carrier-pigeon"}).status_code == 400


def test_saving_the_account_form_keeps_the_connection_method(backup_home):
    _add(via="ssh", ssh_host="ssh.w.test", ssh_user="me", ssh_password="ssh-pw",
         helper_url="https://w.test/avidbackup-abc.php", helper_token="t" * 40, helper_path="/p/avidbackup-abc.php")
    client, token = _client()
    resp = client.post("/accounts/save", data={
        "csrf_token": token, "id": "w.test", "label": "Web Test renamed", "domain": "w.test",
        "site_url": "https://w.test", "enabled": "on", "ftp_protocol": "ftps", "ftp_host": "ftp.w.test",
        "ftp_port": "21", "ftp_user": "u", "ftp_password": "", "ftp_remote_root": "/",
        "db_count": "1", "db_id_0": "main", "db_type_0": "mysql", "db_database_0": "shop", "db_host_0": "localhost",
        "db_user_0": "dbu", "db_port_0": "3306", "db_enabled_0": "on", "db_password_0": "",
    })
    assert resp.status_code == 302
    acct = load_config().account("w.test")
    assert acct.label == "Web Test renamed"  # the form save really happened
    db = acct.databases[0]
    assert (db.via, db.ssh_host, db.ssh_password, db.helper_url, db.helper_path) == (
        "ssh", "ssh.w.test", "ssh-pw", "https://w.test/avidbackup-abc.php", "/p/avidbackup-abc.php")
    assert db.helper_token == "t" * 40


def test_connection_test_button_reports_success_and_failure(backup_home, monkeypatch):
    _add()
    client, token = _client()
    monkeypatch.setattr(db_connection, "test_database_connection", lambda *a: "Connected directly to shop.")
    client.post(PAGE + "/test", data={"csrf_token": token})
    with client.session_transaction() as sess:
        assert ("ok", "Connected directly to shop.") in sess["_flashes"]

    def boom(*a):  # noqa: ANN002
        raise RuntimeError("no route to host")

    monkeypatch.setattr(db_connection, "test_database_connection", boom)
    client.post(PAGE + "/test", data={"csrf_token": token})
    with client.session_transaction() as sess:
        assert any("Connection failed: no route to host" in m for _c, m in sess["_flashes"])


def test_install_and_remove_buttons_update_the_config(backup_home, monkeypatch):
    _add()
    client, token = _client()

    def fake_install(cfg, account, db, **kw):  # noqa: ANN001, ANN003, ANN202
        from avidbackup.php_helper import HelperInfo

        db.via, db.helper_url, db.helper_path, db.helper_token = "php", "https://w.test/avidbackup-z.php", "/x/avidbackup-z.php", "t" * 40
        return HelperInfo(url=db.helper_url, path=db.helper_path, token=db.helper_token)

    def fake_remove(cfg, account, db):  # noqa: ANN001, ANN202
        db.via, db.helper_url, db.helper_path, db.helper_token = "direct", "", "", ""

    monkeypatch.setattr(db_connection, "install_helper_for", fake_install)
    monkeypatch.setattr(db_connection, "remove_helper_for", fake_remove)
    client.post(PAGE.replace("connection", "helper/install"), data={"csrf_token": token})
    assert _db().via == "php" and _db().helper_url.endswith("avidbackup-z.php")
    client.post(PAGE.replace("connection", "helper/remove"), data={"csrf_token": token})
    assert _db().via == "direct" and not _db().helper_url


def test_helper_buttons_are_only_offered_for_mysql(backup_home):
    cfg = load_config()
    upsert_account(cfg, AccountConfig(id="pg.test", label="PG", ftp=FtpConfig(host="h", user="u", password="p"),
                                      databases=[DatabaseConfig(id="main", type="postgres", database="d")]))
    save_config(cfg)
    client, _ = _client()
    html = client.get("/accounts/pg.test/db/main/connection").get_data(as_text=True)
    assert 'value="php"' not in html and "Install the helper" not in html and "SSH tunnel" in html


def test_account_form_links_to_the_connection_page(backup_home):
    _add(via="php", helper_url="https://w.test/avidbackup-abc.php", helper_token="t" * 40)
    client, _ = _client()
    html = client.get("/accounts/w.test/edit").get_data(as_text=True)
    assert "/accounts/w.test/db/main/connection" in html and "PHP helper" in html
