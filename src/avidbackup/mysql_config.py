from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from avidbackup.remote import RemoteClient

log = logging.getLogger(__name__)


@dataclass
class MysqlCredentials:
    host: str
    port: int
    user: str
    password: str
    database: str
    source_path: str
    profile: str = "web"


_DEFINE_RE = re.compile(
    r"""define\s*\(\s*['"](DB_NAME|DB_USER|DB_PASSWORD|DB_HOST|DB_PORT)['"]\s*,\s*['"](.*?)['"]\s*\)""",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGN_RE = re.compile(
    r"""\$(db_username|db_password|databse_name|database_name|db_name|dbname|dbhost|db_host|dbuser|db_user|dbpass|db_pass|dbpassword|host|user|password|name|database|db)\s*=\s*['"]([^'"]*)['"]""",
    re.IGNORECASE,
)
_ENV_RE = re.compile(
    r"""^\s*(DB_HOST|DB_PORT|DB_DATABASE|DB_USERNAME|DB_PASSWORD|MYSQL_HOST|MYSQL_PORT|MYSQL_DATABASE|MYSQL_USER|MYSQL_PASSWORD|DATABASE_URL)\s*=\s*(.*)$""",
    re.IGNORECASE | re.MULTILINE,
)
_JOOMLA_RE = re.compile(
    r"""public\s+\$(host|user|password|db|dbprefix)\s*=\s*['"]([^'"]*)['"]""",
    re.IGNORECASE,
)
# Generic PHP array profile: return [ 'web' => [ 'host'=>.., 'user'=>.., 'pass'=>.., 'name'=>.. ], ... ]
_PHP_PROFILE_RE = re.compile(
    r"""['\"](?P<profile>[^'\"]+)['\"]\s*=>\s*\[(?P<body>.*?)\]""",
    re.S,
)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    return value


def parse_php_array_profile(text: str, profile: str, source_path: str) -> MysqlCredentials | None:
    """Parse a brand-neutral PHP associative array of DB profiles."""
    for m in _PHP_PROFILE_RE.finditer(text):
        if m.group("profile") != profile:
            continue
        block = m.group("body")
        fields: dict[str, str] = {}
        for key in ("host", "user", "pass", "password", "name", "database", "db", "port"):
            km = re.search(rf"['\"]{key}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", block)
            if km:
                fields[key] = km.group(1)
        user = fields.get("user")
        password = fields.get("pass", fields.get("password"))
        name = fields.get("name") or fields.get("database") or fields.get("db")
        if not user or password is None or not name:
            return None
        return MysqlCredentials(
            host=fields.get("host") or "localhost",
            port=int(fields["port"]) if fields.get("port", "").isdigit() else 3306,
            user=user,
            password=password,
            database=name,
            source_path=source_path,
            profile=profile,
        )
    return None


def parse_mysql_credentials(text: str, source_path: str) -> MysqlCredentials | None:
    found: dict[str, str] = {}

    for key, val in _DEFINE_RE.findall(text):
        found[key.upper()] = val

    for key, val in _JOOMLA_RE.findall(text):
        k = key.lower()
        if k == "host":
            found["DB_HOST"] = val
        elif k == "user":
            found["DB_USER"] = val
        elif k == "password":
            found["DB_PASSWORD"] = val
        elif k == "db":
            found["DB_NAME"] = val

    for match in _ENV_RE.finditer(text):
        key = match.group(1).upper()
        val = _strip_quotes(match.group(2))
        if key in {"DB_HOST", "MYSQL_HOST"}:
            found["DB_HOST"] = val
        elif key in {"DB_PORT", "MYSQL_PORT"}:
            found["DB_PORT"] = val
        elif key in {"DB_DATABASE", "MYSQL_DATABASE"}:
            found["DB_NAME"] = val
        elif key in {"DB_USERNAME", "MYSQL_USER"}:
            found["DB_USER"] = val
        elif key in {"DB_PASSWORD", "MYSQL_PASSWORD"}:
            found["DB_PASSWORD"] = val
        elif key == "DATABASE_URL" and val.startswith("mysql"):
            m = re.match(
                r"mysql(?:i)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^?\s]+)",
                val,
            )
            if m:
                found["DB_USER"], found["DB_PASSWORD"], found["DB_HOST"] = (
                    m.group(1),
                    m.group(2),
                    m.group(3),
                )
                if m.group(4):
                    found["DB_PORT"] = m.group(4)
                found["DB_NAME"] = m.group(5)

    if "DB_NAME" not in found or "DB_USER" not in found:
        assigns: dict[str, str] = {}
        for m in _ASSIGN_RE.finditer(text):
            assigns[m.group(1).lower()] = m.group(2)
        mapping = {
            "db_name": "DB_NAME",
            "dbname": "DB_NAME",
            "database": "DB_NAME",
            "database_name": "DB_NAME",
            "databse_name": "DB_NAME",  # common typo in older PHP apps
            "name": "DB_NAME",
            "db": "DB_NAME",
            "db_user": "DB_USER",
            "dbuser": "DB_USER",
            "db_username": "DB_USER",
            "user": "DB_USER",
            "db_pass": "DB_PASSWORD",
            "dbpass": "DB_PASSWORD",
            "dbpassword": "DB_PASSWORD",
            "db_password": "DB_PASSWORD",
            "password": "DB_PASSWORD",
            "db_host": "DB_HOST",
            "dbhost": "DB_HOST",
            "host": "DB_HOST",
        }
        for src, dst in mapping.items():
            if src in assigns and dst not in found:
                found[dst] = assigns[src]

    name = found.get("DB_NAME")
    user = found.get("DB_USER")
    password = found.get("DB_PASSWORD")
    host = found.get("DB_HOST", "localhost")
    if not name or not user or password is None:
        return None

    port = 3306
    host_only = host
    if ":" in host and not host.startswith("/"):
        h, _, p = host.rpartition(":")
        if p.isdigit():
            host_only, port = h, int(p)
    if "DB_PORT" in found and found["DB_PORT"].isdigit():
        port = int(found["DB_PORT"])

    return MysqlCredentials(
        host=host_only or "localhost",
        port=port,
        user=user,
        password=password,
        database=name,
        source_path=source_path,
    )


def discover_mysql_credentials(
    client: RemoteClient,
    remote_root: str,
    candidates: list[str],
    *,
    profile: str = "web",
) -> MysqlCredentials:
    """
    Scan common CMS/app config paths under the remote web root for MySQL credentials.

    Supports WordPress-style define(), Joomla public $props, .env keys, and a generic
    PHP associative array of named profiles (brand-neutral).
    """
    errors: list[str] = []
    for rel in candidates:
        rel = rel.lstrip("/")
        remote_path = f"{remote_root.rstrip('/')}/{rel}" if remote_root != "/" else f"/{rel}"
        if not client.exists(remote_path):
            continue
        try:
            text = client.read_bytes(remote_path).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{remote_path}: read failed ({exc})")
            continue
        creds = parse_php_array_profile(text, profile, remote_path)
        if creds is None:
            creds = parse_mysql_credentials(text, remote_path)
        if creds:
            log.info(
                "MySQL config found in %s (db=%s host=%s user=%s)",
                remote_path,
                creds.database,
                creds.host,
                creds.user,
            )
            return creds
        errors.append(f"{remote_path}: present but no DB credentials parsed")

    detail = "; ".join(errors) if errors else "none of the candidate paths exist"
    raise FileNotFoundError(
        "Could not discover MySQL credentials from website config files. " + detail
    )


def resolve_direct_host(
    creds: MysqlCredentials,
    *,
    mysql_host_override: str,
    ftp_host: str,
) -> str:
    """
    Config often says host=localhost (valid only on the web server).
    For direct dumps from this machine, use an override or the FTP/SFTP hostname.
    """
    override = (mysql_host_override or "").strip()
    if override:
        return override
    host = (creds.host or "").strip().lower()
    if host in {"", "localhost", "127.0.0.1", "::1"}:
        return ftp_host
    return creds.host
