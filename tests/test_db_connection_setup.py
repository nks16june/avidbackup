from __future__ import annotations

import pytest

from avidbackup import db_connection
from avidbackup.php_helper import HelperError, token_sha256
from avidbackup.store import (
    CONNECTION_FIELDS,
    AccountConfig,
    DatabaseConfig,
    FtpConfig,
    carry_connection_settings,
    load_config,
    save_config,
    upsert_account,
)


class _Remote:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a):  # noqa: ANN002
        return False

    def upload_bytes(self, path, data):  # noqa: ANN001
        self.files[path] = data

    def delete_file(self, path):  # noqa: ANN001
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]


@pytest.fixture
def site(backup_home, monkeypatch):
    cfg = load_config()
    acct = AccountConfig(
        id="h.test", label="H", site_url="https://h.test",
        ftp=FtpConfig(host="ftp.h.test", user="u", password="p", remote_root="/public_html"),
        databases=[DatabaseConfig(id="main", type="mysql", host="localhost", user="dbu", password="dbpw",
                                  database="shop")],
    )
    upsert_account(cfg, acct)
    save_config(cfg)
    cfg = load_config()
    remote = _Remote()
    monkeypatch.setattr(db_connection, "connect", lambda settings: remote)
    # skip the real HTTPS check; the client is covered in test_php_helper_client
    monkeypatch.setattr(db_connection, "install_helper", _fake_install(remote))
    return cfg, cfg.account("h.test"), cfg.account("h.test").databases[0], remote


def _fake_install(remote):  # noqa: ANN001, ANN202
    from avidbackup import php_helper

    real = php_helper.install_helper

    class _Ok:
        def __init__(self, *a, **k) -> None:
            pass

        def ping(self):  # noqa: ANN202
            return {"ok": True}

    def run(r, **kw):  # noqa: ANN001, ANN003, ANN202
        original = php_helper.HelperClient
        php_helper.HelperClient = _Ok
        try:
            return real(r, **kw)
        finally:
            php_helper.HelperClient = original

    return run


def test_install_switches_the_database_to_the_php_helper(site):
    cfg, acct, db, remote = site
    info = db_connection.install_helper_for(cfg, acct, db)
    assert db.via == "php" and db.helper_url == info.url and db.helper_path == info.path
    assert info.url.startswith("https://h.test/avidbackup-") and info.path.startswith("/public_html/avidbackup-")
    source = remote.files[info.path].decode()
    assert token_sha256(info.token) in source and info.token not in source
    assert "dbpw" in source and "'shop'" in source  # the helper carries the DB login, the token only its hash
    assert db.helper_token == info.token


def test_reinstall_replaces_the_old_helper_file(site):
    cfg, acct, db, remote = site
    first = db_connection.install_helper_for(cfg, acct, db)
    second = db_connection.install_helper_for(cfg, acct, db)
    assert first.path != second.path and first.token != second.token
    assert list(remote.files) == [second.path]  # the first file is gone


def test_remove_deletes_the_file_and_returns_to_a_direct_connection(site):
    cfg, acct, db, remote = site
    db_connection.install_helper_for(cfg, acct, db)
    db_connection.remove_helper_for(cfg, acct, db)
    assert remote.files == {} and db.via == "direct" and not db.helper_url and not db.helper_token


def test_helper_is_refused_for_other_database_types(site):
    cfg, acct, db, _ = site
    db.type = "postgres"
    with pytest.raises(HelperError, match="MySQL"):
        db_connection.install_helper_for(cfg, acct, db)


def test_connection_settings_survive_saving_the_config(site):
    cfg, acct, db, _ = site
    db.via = "ssh"
    db.ssh_host, db.ssh_user, db.ssh_password, db.ssh_port = "ssh.h.test", "me", "sshpw", 2200
    db.helper_url, db.helper_token = "https://h.test/x.php", "tok" * 12
    save_config(cfg)
    again = load_config().account("h.test").databases[0]
    for name in CONNECTION_FIELDS:
        assert getattr(again, name) == getattr(db, name), name


def test_account_form_save_does_not_wipe_connection_settings(site):
    """The account form rebuilds each database from form fields; it must keep the connection method."""
    _cfg, _acct, old, _ = site
    old.via, old.ssh_host, old.helper_url = "ssh", "ssh.h.test", "https://h.test/x.php"
    rebuilt = DatabaseConfig(id="main", type="mysql", host="localhost", user="dbu", database="shop")
    assert rebuilt.via == "direct"
    kept = carry_connection_settings(old, rebuilt)
    assert (kept.via, kept.ssh_host, kept.helper_url) == ("ssh", "ssh.h.test", "https://h.test/x.php")
    assert carry_connection_settings(None, rebuilt) is rebuilt  # a brand-new database stays direct
