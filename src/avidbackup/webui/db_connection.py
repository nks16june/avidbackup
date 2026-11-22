"""Web page: how AvidBackup reaches a database (direct, SSH tunnel or PHP helper)."""

from __future__ import annotations

import logging

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from avidbackup import db_connection as connection_ops
from avidbackup.audit import write_audit
from avidbackup.php_helper import HelperError
from avidbackup.store import load_config, save_config, upsert_account
from avidbackup.validation import validate_account_id

log = logging.getLogger(__name__)

HOST_KEY_POLICIES = {"tofu", "strict", "insecure"}


def register_db_connection_routes(app: Flask) -> None:
    def _lookup(account_id: str, db_id: str):  # noqa: ANN202
        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
        except (KeyError, ValueError):
            abort(404)
        db = next((d for d in account.databases if d.id == db_id), None)
        if db is None:
            abort(404)
        return cfg, account, db

    def _back(account_id: str, db_id: str):  # noqa: ANN202
        return redirect(url_for("db_connection_page", account_id=account_id, db_id=db_id))

    @app.get("/accounts/<account_id>/db/<db_id>/connection")
    def db_connection_page(account_id: str, db_id: str):  # noqa: ANN202
        cfg, account, db = _lookup(account_id, db_id)
        return render_template(
            "db_connection.html",
            account=account,
            db=db,
            sftp_site=account.ftp.protocol == "sftp",
            has_ssh_password=bool(db.ssh_password or db.ssh_password_env),
            has_token=bool(db.helper_token or db.helper_token_env),
        )

    @app.post("/accounts/<account_id>/db/<db_id>/connection")
    def db_connection_save(account_id: str, db_id: str):  # noqa: ANN202
        cfg, account, db = _lookup(account_id, db_id)
        via = (request.form.get("via") or "direct").strip().lower()
        if via not in {"direct", "ssh", "php"}:
            abort(400)
        if via == "php" and not db.helper_url:
            flash("Install the PHP helper first (the button below), then choose it.", "bad")
            return _back(account_id, db_id)
        if via == "php" and db.type not in {"mysql", "mariadb"}:
            flash("The PHP helper only supports MySQL / MariaDB.", "bad")
            return _back(account_id, db_id)

        if via == "ssh":
            same = request.form.get("ssh_same_as_ftp") == "on"
            host = (request.form.get("ssh_host") or "").strip()
            user = (request.form.get("ssh_user") or "").strip()
            if same and account.ftp.protocol != "sftp":
                flash("Reusing the site login needs the site's file access to be SFTP.", "bad")
                return _back(account_id, db_id)
            if not same and not (host and user):
                flash("Enter the SSH host and user, or reuse the site's SFTP login.", "bad")
                return _back(account_id, db_id)
            try:
                port = int(request.form.get("ssh_port") or 22)
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                flash("The SSH port must be a number from 1 to 65535.", "bad")
                return _back(account_id, db_id)
            policy = (request.form.get("ssh_host_key_policy") or "tofu").strip().lower()
            db.ssh_same_as_ftp = same
            db.ssh_host, db.ssh_user, db.ssh_port = host, user, port
            db.ssh_host_key_policy = policy if policy in HOST_KEY_POLICIES else "tofu"
            db.ssh_key_file = (request.form.get("ssh_key_file") or "").strip()
            db.ssh_password_env = (request.form.get("ssh_password_env") or "").strip()
            password = request.form.get("ssh_password") or ""
            if password:  # blank keeps the saved one; the saved value is never sent to the page
                db.ssh_password = password
            # the database address as seen from the SSH server
            db.host = (request.form.get("db_host") or "").strip() or "127.0.0.1"
            try:
                db.port = int(request.form.get("db_port") or db.port or 3306)
            except ValueError:
                db.port = 3306
        elif via == "php":
            db.host = (request.form.get("db_host") or "").strip() or db.host or "localhost"

        db.via = via
        upsert_account(cfg, account)
        save_config(cfg)
        write_audit("db_connection_changed", account=account_id, db=db_id, via=via)
        flash(f"Saved: this database is reached {_describe(via)}.", "ok")
        return _back(account_id, db_id)

    @app.post("/accounts/<account_id>/db/<db_id>/connection/test")
    def db_connection_test(account_id: str, db_id: str):  # noqa: ANN202
        cfg, account, db = _lookup(account_id, db_id)
        try:
            flash(connection_ops.test_database_connection(cfg, account, db), "ok")
        except Exception as exc:  # noqa: BLE001
            flash(f"Connection failed: {exc}", "bad")
        return _back(account_id, db_id)

    @app.post("/accounts/<account_id>/db/<db_id>/helper/install")
    def db_helper_install(account_id: str, db_id: str):  # noqa: ANN202
        cfg, account, db = _lookup(account_id, db_id)
        try:
            info = connection_ops.install_helper_for(
                cfg,
                account,
                db,
                base_url=(request.form.get("helper_base_url") or "").strip(),
                allowed_ips=(request.form.get("allowed_ips") or "").strip(),
            )
        except (HelperError, OSError, ValueError) as exc:
            flash(f"Could not install the helper: {exc}", "bad")
            return _back(account_id, db_id)
        upsert_account(cfg, account)
        save_config(cfg)
        write_audit("helper_installed", account=account_id, db=db_id, path=info.path)
        flash(f"Helper installed and answering at {info.url}. This database now uses it.", "ok")
        return _back(account_id, db_id)

    @app.post("/accounts/<account_id>/db/<db_id>/helper/remove")
    def db_helper_remove(account_id: str, db_id: str):  # noqa: ANN202
        cfg, account, db = _lookup(account_id, db_id)
        path = db.helper_path
        try:
            connection_ops.remove_helper_for(cfg, account, db)
        except (HelperError, OSError, ValueError) as exc:
            flash(f"Could not remove the helper: {exc}", "bad")
            return _back(account_id, db_id)
        upsert_account(cfg, account)
        save_config(cfg)
        write_audit("helper_removed", account=account_id, db=db_id, path=path)
        flash("Helper removed from the server. This database connects directly again.", "ok")
        return _back(account_id, db_id)


def _describe(via: str) -> str:
    return {"direct": "directly", "ssh": "through an SSH tunnel", "php": "through the PHP helper"}[via]
