from __future__ import annotations

import ipaddress
import logging
import os
import secrets

from flask import Flask, Response, abort, redirect, request, session, url_for

from avidbackup.paths import ui_secret_key_path

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def load_or_create_secret_key() -> str:
    path = ui_secret_key_path()
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def register_security(app: Flask, *, ui_token: str = "") -> None:
    token = (ui_token or os.environ.get("AVIDBACKUP_UI_TOKEN") or "").strip()
    app.config["AVIDBACKUP_UI_TOKEN"] = token

    @app.before_request
    def _csrf_and_auth():  # type: ignore[misc]
        if request.endpoint == "static":
            return None
        # CSRF for state-changing methods
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            sess_tok = session.get("csrf_token")
            form_tok = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not sess_tok or not form_tok or not secrets.compare_digest(str(sess_tok), str(form_tok)):
                abort(400, description="CSRF validation failed")
        # Optional UI token for non-loopback or when configured
        if token:
            authed = session.get("ui_authed") is True
            if not authed and request.endpoint not in {"login", "login_submit"}:
                return redirect(url_for("login", next=request.path))
        return None

    @app.after_request
    def _security_headers(resp: Response):  # type: ignore[misc]
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "script-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return resp

    @app.context_processor
    def _csrf_token():  # type: ignore[misc]
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return {"csrf_token": session["csrf_token"]}


def require_ui_token_match(provided: str, expected: str) -> bool:
    if not expected:
        return True
    return secrets.compare_digest(provided.strip(), expected.strip())
