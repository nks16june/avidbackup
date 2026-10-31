from __future__ import annotations

import pytest

from avidbackup.main import run
from avidbackup.validation import validate_account_id
from avidbackup.webui.app import create_app


def test_validator_rejects_dotdot():
    with pytest.raises(ValueError, match="traversal"):
        validate_account_id("..")
    with pytest.raises(ValueError):
        validate_account_id("../evil")
    with pytest.raises(ValueError):
        validate_account_id("a/b")
    with pytest.raises(ValueError, match="traversal"):
        validate_account_id("evil..id")


def test_ui_edit_rejects_dotdot(backup_home):
    app = create_app()
    client = app.test_client()
    resp = client.get("/accounts/%2e%2e/edit", follow_redirects=True)
    assert resp.status_code == 200
    # Landed on index after flash (invalid id), not the edit form for ".."
    assert b"Add website" in resp.data or b"Accounts" in resp.data or b"AvidBackup" in resp.data
    assert b'name="ftp_host"' not in resp.data


def test_ui_run_rejects_dotdot(backup_home):
    app = create_app()
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as sess:
        tok = sess["csrf_token"]
    resp = client.post(
        "/accounts/%2e%2e/run",
        data={"csrf_token": tok},
        follow_redirects=True,
    )
    assert resp.status_code == 200


def test_cli_rejects_traversal(backup_home):
    assert run(["run", "--account", "../evil"]) == 2
    assert run(["restore-db", "--account", "..", "--db", "main"]) == 2
