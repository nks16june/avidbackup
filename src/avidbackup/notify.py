from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from avidbackup.secure_cli import resolve_secret
from avidbackup.store import AppConfig, NotifyConfig

log = logging.getLogger(__name__)


def send_success(cfg: AppConfig, message: str) -> bool:
    """Optional webhook and/or SMTP notify. Both off by default."""
    ok = False
    n = cfg.notify
    if n.webhook_url:
        ok = _webhook(n, message) or ok
    if n.smtp_host and n.smtp_to and n.smtp_from:
        ok = _smtp(n, message) or ok
    return ok


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


def _smtp(n: NotifyConfig, message: str) -> bool:
    try:
        password = resolve_secret(n.smtp_password, n.smtp_password_env)
        msg = EmailMessage()
        msg["Subject"] = "AvidBackup notification"
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
