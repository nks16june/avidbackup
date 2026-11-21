from __future__ import annotations

import json
import logging
import secrets
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from urllib.parse import urlsplit

from avidbackup.secure_cli import resolve_secret
from avidbackup.store import AppConfig, NotifyConfig

log = logging.getLogger(__name__)

PUBLIC_NTFY_HOSTS = ("ntfy.sh",)
_GENERIC_BODY = {
    False: "A backup run finished. Open AvidBackup to see the details.",
    True: "Attention needed on at least one site. Open AvidBackup to see the details.",
}


def send_success(cfg: AppConfig, message: str) -> bool:
    """Send a backup status to the configured phone, webhook, and email channels."""
    return send_status(cfg, "AvidBackup", message, urgent=False)


def channels_configured(cfg: AppConfig) -> list[str]:
    """Names of the notification channels the user has set up (empty by default)."""
    n = cfg.notify
    channels: list[str] = []
    if n.phone_enabled and n.ntfy_topic:
        channels.append("phone")
    if n.webhook_url:
        channels.append("webhook")
    if n.smtp_host and n.smtp_to and n.smtp_from:
        channels.append("email")
    return channels


def send_status(cfg: AppConfig, title: str, message: str, *, urgent: bool) -> bool:
    """
    Phone (ntfy, opt-in), plus optional webhook and email.

    Returns True when at least one channel accepted the message. With no channel set up
    nothing is sent and nothing is logged as a problem; a warning is only logged when a
    channel was configured and every configured channel failed.
    """
    channels = channels_configured(cfg)
    if not channels:
        log.debug("No notification channel configured; nothing to send")
        return False
    n = cfg.notify
    ok = False
    if "phone" in channels:
        ok = _ntfy(n, title, message, urgent=urgent) or ok
    if "webhook" in channels:
        ok = _webhook(n, message) or ok
    if "email" in channels:
        ok = _smtp(n, title, message) or ok
    if not ok:
        log.warning("Notification failed on every configured channel: %s", ", ".join(channels))
    return ok


def phone_subscribe_url(cfg: AppConfig) -> str:
    n = cfg.notify
    if not n.ntfy_topic:
        return ""
    return f"{n.ntfy_server.rstrip('/')}/{n.ntfy_topic}"


def ensure_phone_config(cfg: AppConfig) -> AppConfig:
    """Fill a private (random) ntfy topic once phone alerts are switched on."""
    from avidbackup.store import save_config

    n = cfg.notify
    if not n.phone_enabled:
        return cfg
    changed = False
    if not n.ntfy_server:
        n.ntfy_server = "https://ntfy.sh"
        changed = True
    if not n.ntfy_topic:
        n.ntfy_topic = secrets.token_urlsafe(18)
        changed = True
    if changed:
        save_config(cfg)
        log.info("Phone alerts saved. Subscribe on your phone: %s", phone_subscribe_url(cfg))
    return cfg


def uses_public_ntfy(n: NotifyConfig) -> bool:
    host = (urlsplit(n.ntfy_server).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in PUBLIC_NTFY_HOSTS)


def _ntfy(n: NotifyConfig, title: str, message: str, *, urgent: bool) -> bool:
    if uses_public_ntfy(n) and not n.ntfy_details:
        # Third-party server: send no site names, paths or counts.
        title = "AvidBackup: attention needed" if urgent else "AvidBackup"
        message = _GENERIC_BODY[bool(urgent)]
    url = f"{n.ntfy_server.rstrip('/')}/{n.ntfy_topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high" if urgent else "default",
            "Tags": "warning" if urgent else "white_check_mark",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("Phone (ntfy) status=%s", getattr(resp, "status", "?"))
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except urllib.error.URLError as exc:
        log.error("Phone (ntfy) failed: %s", exc)
        return False


def _webhook(n: NotifyConfig, message: str) -> bool:
    payload = json.dumps({"text": message, "source": "avidbackup"}).encode("utf-8")
    req = urllib.request.Request(
        n.webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if n.webhook_secret:
        req.add_header("Authorization", f"Bearer {n.webhook_secret}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("Webhook notify status=%s", getattr(resp, "status", "?"))
            return True
    except urllib.error.URLError as exc:
        log.error("Webhook notify failed: %s", exc)
        return False


def _smtp(n: NotifyConfig, title: str, message: str) -> bool:
    try:
        password = resolve_secret(n.smtp_password, n.smtp_password_env)
        msg = EmailMessage()
        msg["Subject"] = title or "AvidBackup notification"
        msg["From"] = n.smtp_from
        msg["To"] = n.smtp_to
        msg.set_content(message)
        if n.smtp_tls:
            with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                if n.smtp_user:
                    smtp.login(n.smtp_user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=30) as smtp:
                if n.smtp_user:
                    smtp.login(n.smtp_user, password)
                smtp.send_message(msg)
        log.info("SMTP notify sent to %s", n.smtp_to)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("SMTP notify failed: %s", exc)
        return False
