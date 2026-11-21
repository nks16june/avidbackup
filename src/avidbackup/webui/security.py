from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, request, session, url_for

from avidbackup.paths import ui_secret_key_path, ui_token_path

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_AUTH_OFF_VALUES = {"off", "0", "false", "no"}

# Failed sign-ins allowed per client in the window before it is locked out.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60


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


def load_or_create_ui_token(*, rotate: bool = False) -> str:
    """Random sign-in token stored next to the UI secret key (generated on first run)."""
    path = ui_token_path()
    if path.is_file() and not rotate:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def auth_disabled_by_env() -> bool:
    return os.environ.get("AVIDBACKUP_UI_AUTH", "").strip().lower() in _AUTH_OFF_VALUES


def resolve_ui_token(configured: str = "") -> str:
    """
    Token the UI requires.

    Explicit config / AVIDBACKUP_UI_TOKEN wins. Otherwise a random token is generated on
    first run, unless the user opted out with AVIDBACKUP_UI_AUTH=off (loopback only).
    """
    explicit = (configured or os.environ.get("AVIDBACKUP_UI_TOKEN") or "").strip()
    if explicit:
        return explicit
    if auth_disabled_by_env():
        return ""
    return load_or_create_ui_token()


def signin_url(base_url: str, token: str) -> str:
    """URL that signs in without the token ever reaching the server log (fragment only)."""
    base = base_url.rstrip("/")
    return f"{base}/login#t={token}" if token else f"{base}/"


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def hostname_of(host_header: str) -> str:
    """Lower-cased host name from a Host header, without port or IPv6 brackets."""
    value = (host_header or "").strip().lower()
    if not value:
        return ""
    try:
        parts = urlsplit(f"//{value}")
        return (parts.hostname or "").strip()
    except ValueError:
        return ""


def host_allowed(host_header: str, extra: set[str] | None = None) -> bool:
    """
    DNS-rebinding guard: only loopback names are accepted unless explicitly allowed.

    A malicious site can point its own domain at 127.0.0.1; the browser then sends that
    domain in the Host header, which is rejected here.
    """
    name = hostname_of(host_header)
    if not name:
        return False
    if is_loopback_host(name):
        return True
    return name in {h.lower() for h in (extra or set())}


def safe_next(value: str | None, default: str = "/") -> str:
    """Only same-site absolute paths; blocks open redirects such as //evil.test or http://x."""
    raw = (value or "").strip()
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return default
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return default
    if any(ord(c) < 32 for c in raw):
        return default
    return raw


class LoginLimiter:
    """Small in-memory brute-force guard for the sign-in form."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: int = LOGIN_WINDOW_SECONDS):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str, now: float | None = None) -> bool:
        return len(self._recent(key, now if now is not None else time.monotonic())) >= self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def register_security(
    app: Flask,
    *,
    ui_token: str = "",
    allow_nonloopback: bool = False,
    extra_hosts: set[str] | None = None,
) -> None:
    """
    Wire host checking, CSRF, optional sign-in, cookies and security headers.

    ``allow_nonloopback`` is set when the server is deliberately bound to a LAN address
    (Docker, NAS). A token is then mandatory, so the Host check is relaxed.
    """
    token = (ui_token or "").strip()
    app.config["AVIDBACKUP_UI_TOKEN"] = token
    app.config["AVIDBACKUP_LOGIN_LIMITER"] = LoginLimiter()
    hosts = {h.strip().lower() for h in (extra_hosts or set()) if h.strip()}
    env_hosts = os.environ.get("AVIDBACKUP_ALLOWED_HOSTS", "")
    hosts |= {h.strip().lower() for h in env_hosts.split(",") if h.strip()}

    app.config.update(
        SESSION_COOKIE_NAME="avidbackup_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    @app.before_request
    def _guard():  # type: ignore[misc]
        if not (allow_nonloopback and token) and not host_allowed(request.host, hosts):
            log.warning("Rejected request with Host %r", request.host)
            abort(400, description="Invalid Host header")
        if request.endpoint == "static":
            return None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and origin != "null":
                if hostname_of(urlsplit(origin).netloc) != hostname_of(request.host):
                    abort(400, description="Cross-origin request blocked")
            elif origin == "null":
                abort(400, description="Cross-origin request blocked")
            sess_tok = session.get("csrf_token")
            form_tok = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if (
                not sess_tok
                or not form_tok
                or not secrets.compare_digest(str(sess_tok), str(form_tok))
            ):
                abort(400, description="CSRF validation failed")
        if token:
            authed = session.get("ui_authed") is True and session.get("ui_tok") == token_fingerprint(token)
            if not authed and request.endpoint not in {"login", "login_submit"}:
                return redirect(url_for("login", next=safe_next(request.full_path.rstrip("?"))))
        return None

    @app.after_request
    def _security_headers(resp: Response):  # type: ignore[misc]
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # "same-origin", not "no-referrer": browsers send "Origin: null" on form posts under
        # no-referrer, which the Origin check (rightly) rejects. same-origin still sends
        # nothing to other sites.
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control") or "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
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
