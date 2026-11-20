from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, session, url_for

from avidbackup.backup_report import build_report, human_bytes
from avidbackup.initial_full import has_completed_initial_full
from avidbackup.paths import app_root, ensure_layout
from avidbackup.pgtools import PgToolsError, install_postgres_tools
from avidbackup.procutil import cli_command
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import (
    AccountConfig,
    DatabaseConfig,
    FtpConfig,
    RetentionConfig,
    delete_account,
    load_config,
    new_account_id,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.util_bin import find_pg_dump, find_psql
from avidbackup.validation import validate_account_id
from avidbackup.webui.security import (
    is_loopback_host,
    load_or_create_secret_key,
    register_security,
    require_ui_token_match,
)

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent / "templates"


def create_app() -> Flask:
    ensure_layout()
    cfg0 = load_config()
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.secret_key = load_or_create_secret_key()
    register_security(app, ui_token=cfg0.ui_token or os.environ.get("AVIDBACKUP_UI_TOKEN", ""))

    app.jinja_env.filters["human_bytes"] = human_bytes

    @app.context_processor
    def inject_brand():
        return {
            "brand": "AvidBackup",
            "vendor": "Avid Web Solutions",
            "author": "Nishant Shrivastava",
            "home": str(app_root()),
        }

    @app.get("/login")
    def login():
        return render_template("login.html", next=request.args.get("next") or "/")

    @app.post("/login")
    def login_submit():
        expected = app.config.get("AVIDBACKUP_UI_TOKEN") or ""
        provided = request.form.get("token") or ""
        if require_ui_token_match(provided, expected):
            session["ui_authed"] = True
            flash("Signed in.", "ok")
            return redirect(request.form.get("next") or url_for("index"))
        flash("Invalid UI token.", "bad")
        return render_template("login.html", next=request.form.get("next") or "/"), 401

    @app.get("/")
    def index():
        cfg = load_config()
        rows = []
        for a in cfg.accounts:
            try:
                s = settings_from_account(cfg, a)
                initial = has_completed_initial_full(s)
                data_dir = str(s.local_backup_root)
            except Exception:  # noqa: BLE001
                initial = False
                data_dir = str(resolve_data_dir(cfg, a))
            rows.append(
                {
                    "account": a,
                    "initial_done": initial,
                    "data_dir": data_dir,
                    "db_count": len([d for d in a.databases if d.enabled]),
                }
            )
        return render_template("index.html", cfg=cfg, rows=rows)

    @app.get("/backups")
    def backups():
        cfg = load_config()
        try:
            days = int(request.args.get("days", 30))
        except ValueError:
            days = 30
        days = days if days in (7, 30, 90) else 30
        account = (request.args.get("account") or "").strip()
        if account:
            try:
                validate_account_id(account)
                cfg.account(account)
            except (KeyError, ValueError):
                flash("Unknown account.", "bad")
                return redirect(url_for("backups"))
        report = build_report(cfg, days=days, account_id=account or None)
        return render_template(
            "backups.html", report=report, days=days, account=account, accounts=cfg.accounts
        )

    @app.get("/settings")
    def global_settings():
        return render_template(
            "settings.html",
            cfg=load_config(),
            pg_tools={
                "pg_dump": find_pg_dump(),
                "psql": find_psql(),
                "windows": sys.platform == "win32",
            },
        )

    @app.post("/tools/install-postgres")
    def install_postgres():
        try:
            install_postgres_tools()
            flash("PostgreSQL tools installed.", "ok")
        except PgToolsError as exc:
            flash(f"Could not install PostgreSQL tools: {exc}", "bad")
        return redirect(url_for("global_settings"))

    @app.post("/settings")
    def save_global_settings():
        cfg = load_config()
        cfg.data_root = request.form.get("data_root", cfg.data_root).strip()
        cfg.mysql_retention_days = int(request.form.get("mysql_retention_days") or 15)
        cfg.files_keep_forever = request.form.get("files_keep_forever") == "on"
        cfg.large_table_mb = float(request.form.get("large_table_mb") or 50)
        cfg.download_retries = int(request.form.get("download_retries") or 3)
        cfg.compress_dumps = (request.form.get("compress_dumps") or "gzip").strip()
        cfg.ui_host = request.form.get("ui_host", cfg.ui_host).strip() or "127.0.0.1"
        cfg.ui_port = int(request.form.get("ui_port") or 8787)
        new_token = (request.form.get("ui_token") or "").strip()
        if new_token:
            cfg.ui_token = new_token
        # notify
        cfg.notify.webhook_url = (request.form.get("webhook_url") or "").strip()
        cfg.notify.webhook_secret = (request.form.get("webhook_secret") or "").strip() or cfg.notify.webhook_secret
        cfg.notify.smtp_host = (request.form.get("smtp_host") or "").strip()
        cfg.notify.smtp_port = int(request.form.get("smtp_port") or 587)
        cfg.notify.smtp_user = (request.form.get("smtp_user") or "").strip()
        smtp_pass = request.form.get("smtp_password") or ""
        if smtp_pass:
            cfg.notify.smtp_password = smtp_pass
        cfg.notify.smtp_from = (request.form.get("smtp_from") or "").strip()
        cfg.notify.smtp_to = (request.form.get("smtp_to") or "").strip()
        save_config(cfg)
        flash("Global settings saved.", "ok")
        return redirect(url_for("global_settings"))

    @app.get("/accounts/new")
    def new_account():
        return render_template("account_form.html", account=None, title="Add website account")

    @app.get("/accounts/<account_id>/edit")
    def edit_account(account_id: str):
        try:
            validate_account_id(account_id)
            account = load_config().account(account_id)
        except (KeyError, ValueError):
            flash("Account not found or invalid id.", "bad")
            return redirect(url_for("index"))
        return render_template("account_form.html", account=account, title=f"Edit {account.label}")

    @app.post("/accounts/save")
    def save_account():
        cfg = load_config()
        try:
            raw_id = (request.form.get("id") or "").strip()
            label = (request.form.get("label") or "").strip()
            domain = (request.form.get("domain") or label or raw_id).strip()
            account_id = validate_account_id(raw_id or new_account_id(domain or label or "site", cfg.accounts))
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("new_account"))

        site_url = (request.form.get("site_url") or f"https://{domain}").strip()
        enabled = request.form.get("enabled") == "on"
        existing = None
        try:
            existing = cfg.account(account_id)
        except KeyError:
            pass

        ftp = FtpConfig(
            protocol=(request.form.get("ftp_protocol") or "ftps").strip().lower(),
            host=(request.form.get("ftp_host") or "").strip(),
            port=int(request.form.get("ftp_port") or 21),
            user=(request.form.get("ftp_user") or "").strip(),
            password=request.form.get("ftp_password") or "",
            password_env=(request.form.get("ftp_password_env") or "").strip(),
            tls_explicit=request.form.get("ftp_tls") == "on",
            passive=request.form.get("ftp_passive") == "on",
            remote_root=(request.form.get("ftp_remote_root") or "/public_html").strip(),
            verify_tls=request.form.get("ftp_verify_tls") == "on",
            host_key_policy=(request.form.get("ftp_host_key_policy") or "tofu").strip().lower(),
        )
        if not ftp.password and existing and existing.ftp.password:
            ftp.password = existing.ftp.password
        if not ftp.password_env and existing:
            ftp.password_env = existing.ftp.password_env

        databases: list[DatabaseConfig] = []
        db_count = int(request.form.get("db_count") or 0)
        for i in range(db_count):
            db_type = (request.form.get(f"db_type_{i}") or "mysql").strip().lower()
            db_name = (request.form.get(f"db_database_{i}") or "").strip()
            db_host = (request.form.get(f"db_host_{i}") or "").strip()
            db_path = (request.form.get(f"db_path_{i}") or "").strip()
            if not db_name and not db_host and not db_path and request.form.get(f"db_discover_{i}") != "on":
                continue
            db_id = (request.form.get(f"db_id_{i}") or "").strip() or f"db{i+1}"
            password = request.form.get(f"db_password_{i}") or ""
            password_env = (request.form.get(f"db_password_env_{i}") or "").strip()
            if existing and not password:
                for old in existing.databases:
                    if old.id == db_id and old.password:
                        password = old.password
                        break
            if existing and not password_env:
                for old in existing.databases:
                    if old.id == db_id and old.password_env:
                        password_env = old.password_env
                        break
            default_port = 5432 if db_type == "postgres" else (0 if db_type == "sqlite" else 3306)
            databases.append(
                DatabaseConfig(
                    id=db_id,
                    label=(request.form.get(f"db_label_{i}") or db_name or db_id).strip(),
                    type=db_type,
                    host=db_host,
                    port=int(request.form.get(f"db_port_{i}") or default_port),
                    user=(request.form.get(f"db_user_{i}") or "").strip(),
                    password=password,
                    password_env=password_env,
                    database=db_name,
                    path=db_path,
                    format=(request.form.get(f"db_format_{i}") or "plain").strip(),
                    compress=(request.form.get(f"db_compress_{i}") or cfg.compress_dumps or "gzip").strip(),
                    enabled=request.form.get(f"db_enabled_{i}") == "on",
                    discover_from_site=request.form.get(f"db_discover_{i}") == "on",
                    ssl=request.form.get(f"db_ssl_{i}") == "on",
                )
            )

        retention = RetentionConfig(
            mysql_days=int(request.form.get("mysql_days") or cfg.mysql_retention_days),
            files_forever=request.form.get("files_forever") == "on",
            files_days=int(request.form.get("files_days") or 0),
        )
        account = AccountConfig(
            id=account_id,
            label=label or account_id,
            domain=domain or account_id,
            site_url=site_url,
            enabled=enabled,
            ftp=ftp,
            databases=databases,
            retention=retention,
        )
        if not ftp.host or not ftp.user:
            flash("FTP host and user are required.", "bad")
            return render_template("account_form.html", account=account, title="Fix account")

        upsert_account(cfg, account)
        save_config(cfg)
        flash(f"Saved account {account_id}.", "ok")
        return redirect(url_for("index"))

    @app.post("/accounts/<account_id>/delete")
    def remove_account(account_id: str):
        try:
            validate_account_id(account_id)
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))
        cfg = load_config()
        delete_account(cfg, account_id)
        save_config(cfg)
        flash(f"Removed {account_id} from config (local data folder kept).", "ok")
        return redirect(url_for("index"))

    @app.post("/accounts/<account_id>/run")
    def run_backup(account_id: str):
        try:
            validate_account_id(account_id)
            load_config().account(account_id)
        except (KeyError, ValueError) as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))

        def _job() -> None:
            subprocess.run(
                cli_command("run", "--account", account_id),
                cwd=str(app_root()),
                check=False,
            )

        threading.Thread(target=_job, daemon=True).start()
        flash(f"Backup started for {account_id} in the background.", "ok")
        return redirect(url_for("index"))

    @app.post("/run-all")
    def run_all():
        def _job() -> None:
            subprocess.run(
                cli_command("run"),
                cwd=str(app_root()),
                check=False,
            )

        threading.Thread(target=_job, daemon=True).start()
        flash("Backup started for all enabled accounts.", "ok")
        return redirect(url_for("index"))

    return app


def run_ui(*, host: str = "127.0.0.1", port: int = 8787) -> int:
    cfg = load_config()
    token = (cfg.ui_token or os.environ.get("AVIDBACKUP_UI_TOKEN") or "").strip()
    if not is_loopback_host(host):
        if not token:
            print(
                f"ERROR: Refusing to bind UI to non-loopback host {host!r} without "
                "ui_token / AVIDBACKUP_UI_TOKEN. Set a token in settings or env, or bind to 127.0.0.1.",
                file=sys.stderr,
            )
            return 2
        print(
            f"WARNING: Binding AvidBackup UI to {host}:{port} (non-loopback). "
            "UI token authentication is required. Prefer SSH tunnel + 127.0.0.1.",
            file=sys.stderr,
        )
    app = create_app()
    print(f"AvidBackup UI -> http://{host}:{port}")
    print(f"Home: {app_root()}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0
