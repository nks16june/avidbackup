"""PHP helper: back up a MySQL database on a host that blocks remote MySQL and has no SSH.

AvidBackup generates a small PHP file, uploads it to the site over the same FTP/SFTP login
(random file name, secret token whose SHA-256 is the only thing stored on the server), and then
downloads a gzip-compressed SQL dump over HTTPS. The helper is read-only and runs no shell
commands. See docs/HOSTS-WITHOUT-REMOTE-MYSQL.md for the security notes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HELPER_VERSION = "1"
TEMPLATE_PATH = Path(__file__).resolve().parent / "data" / "avidbackup-helper.php"
DUMP_MARKER = b"-- Dump completed"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class HelperError(RuntimeError):
    pass


@dataclass
class HelperInfo:
    url: str
    path: str  # where the file was uploaded on the server
    token: str


# ---- generating the PHP file -----------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def helper_filename() -> str:
    return f"avidbackup-{secrets.token_hex(8)}.php"


def _php_string(value: str) -> str:
    """Escape for use inside a single-quoted PHP string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def generate_helper(
    *,
    token: str,
    db_host: str,
    db_port: int,
    db_user: str,
    db_password: str,
    db_name: str,
    allowed_ips: str = "",
) -> str:
    """Return the PHP source for one database, locked to `token`."""
    if not token or len(token) < 20:
        raise HelperError("The helper token is too short.")
    for label, value in (("database name", db_name), ("database user", db_user)):
        if not value:
            raise HelperError(f"The {label} is required to create the helper.")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "__AVB_VERSION__": HELPER_VERSION,
        "__AVB_CREATED__": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "__AVB_TOKEN_SHA256__": token_sha256(token),
        "__AVB_DB_HOST__": _php_string(db_host or "localhost"),
        "__AVB_DB_PORT__": str(int(db_port or 3306)),
        "__AVB_DB_USER__": _php_string(db_user),
        "__AVB_DB_PASS__": _php_string(db_password),
        "__AVB_DB_NAME__": _php_string(db_name),
        "__AVB_ALLOWED_IPS__": _php_string(allowed_ips.replace(" ", "")),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    if "__AVB_" in template:
        raise HelperError("Internal error: the helper template still has placeholders.")
    return template


# ---- talking to the helper -------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise HelperError(
            f"The helper address redirects to {newurl}. Use that final address (including https://) "
            "as the helper URL; redirects would drop the secret token."
        )


def _check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise HelperError("The helper URL must start with https://")
    if parts.scheme == "http" and (parts.hostname or "").lower() not in _LOOPBACK:
        raise HelperError(
            "The helper URL must use https://. Sending the secret token over plain http would "
            "expose it. Enable HTTPS for the site first."
        )
    return url.strip()


class HelperClient:
    def __init__(self, url: str, token: str, *, timeout: float = 120.0) -> None:
        self.url = _check_url(url)
        if not token:
            raise HelperError("No helper token is configured.")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _post(self, action: str):  # noqa: ANN202
        body = urllib.parse.urlencode({"token": self.token, "action": action}).encode("ascii")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AvidBackup"},
        )
        try:
            return self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise HelperError(self._explain(exc)) from exc
        except urllib.error.URLError as exc:
            raise HelperError(f"Could not reach the helper at {self.url}: {exc.reason}") from exc
        except OSError as exc:
            raise HelperError(f"Could not reach the helper at {self.url}: {exc}") from exc

    @staticmethod
    def _explain(exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            raw = exc.read(2000)
            detail = json.loads(raw.decode("utf-8", "replace")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        hints = {
            403: "the secret token was rejected (or this address is not allowed)",
            404: "no helper was found at that address (wrong URL, or the file was removed)",
            405: "that address is not the AvidBackup helper",
            429: "too many wrong tokens were sent; the helper is locked for a few minutes",
            501: "the server's PHP has no mysqli extension",
            502: "the helper could not connect to the database",
        }
        text = detail or hints.get(exc.code, exc.reason)
        return f"Helper replied HTTP {exc.code}: {text}"

    def _json(self, action: str) -> dict[str, Any]:
        with self._post(action) as resp:
            raw = resp.read(4_000_000)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HelperError(
                "That address did not return an AvidBackup helper response. Check the URL."
            ) from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise HelperError(str(data.get("error") if isinstance(data, dict) else data))
        return data

    def ping(self) -> dict[str, Any]:
        return self._json("ping")

    def check(self) -> dict[str, Any]:
        return self._json("check")

    def tables(self) -> list[dict[str, Any]]:
        return list(self._json("tables").get("tables", []))

    def dump_to(self, dest: Path) -> int:
        """Stream the dump to `dest` as plain SQL. Returns the bytes written."""
        written = 0
        tail = b""
        decomp = None
        first = True
        tmp = dest.with_name(dest.name + ".part")
        try:
            with self._post("dump") as resp, tmp.open("wb") as out:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "json" in ctype:  # an error reply, not a dump
                    detail = json.loads(resp.read(100000).decode("utf-8", "replace"))
                    raise HelperError(str(detail.get("error", detail)))
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    if first:
                        first = False
                        if chunk[:2] == b"\x1f\x8b":
                            decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    data = decomp.decompress(chunk) if decomp else chunk
                    out.write(data)
                    written += len(data)
                    tail = (tail + data)[-512:]
                if decomp is not None:
                    data = decomp.flush()
                    out.write(data)
                    written += len(data)
                    tail = (tail + data)[-512:]
                    if not decomp.eof:
                        raise HelperError("The download was cut off before it finished (incomplete gzip).")
        except zlib.error as exc:
            tmp.unlink(missing_ok=True)
            raise HelperError(f"The dump could not be decompressed: {exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if DUMP_MARKER not in tail:
            tmp.unlink(missing_ok=True)
            raise HelperError("The dump is incomplete (no completion marker). It was discarded.")
        tmp.replace(dest)
        return written


# ---- installing / removing on the server ------------------------------------------------------


def helper_url_for(site_url: str, filename: str) -> str:
    base = (site_url or "").strip().rstrip("/")
    if not base:
        raise HelperError("Set the site URL (for example https://example.com) first.")
    return f"{base}/{filename}"


def install_helper(
    remote,  # noqa: ANN001  (a connected RemoteClient)
    *,
    web_root: str,
    site_url: str,
    token: str,
    db_host: str,
    db_port: int,
    db_user: str,
    db_password: str,
    db_name: str,
    url: str = "",
    allowed_ips: str = "",
    verify_attempts: int = 4,
) -> HelperInfo:
    """Upload a new helper, prove it answers over HTTPS, and return how to reach it."""
    filename = helper_filename()
    remote_path = f"{web_root.rstrip('/')}/{filename}"
    final_url = _check_url(url) if url else _check_url(helper_url_for(site_url, filename))
    source = generate_helper(
        token=token,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=db_name,
        allowed_ips=allowed_ips,
    )
    remote.upload_bytes(remote_path, source.encode("utf-8"))
    log.info("Uploaded AvidBackup helper to %s", remote_path)
    if url and not final_url.rstrip("/").endswith(filename):
        final_url = final_url.rstrip("/") + "/" + filename
    last: Exception | None = None
    for attempt in range(max(1, verify_attempts)):
        try:
            HelperClient(final_url, token, timeout=30).ping()
            return HelperInfo(url=final_url, path=remote_path, token=token)
        except HelperError as exc:
            last = exc
            if attempt + 1 < verify_attempts:
                time.sleep(1.5)
    try:  # do not leave a helper behind that cannot be reached
        remote.delete_file(remote_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not remove the unreachable helper %s: %s", remote_path, exc)
    raise HelperError(
        f"The helper was uploaded but did not answer at {final_url}: {last}. "
        "Check that the site URL matches the folder AvidBackup uploads to, that PHP is enabled, "
        "and that the site works over https. The file was removed again."
    )


def remove_helper(remote, remote_path: str) -> None:  # noqa: ANN001
    if not remote_path:
        raise HelperError("No helper path is recorded for this database.")
    remote.delete_file(remote_path)
