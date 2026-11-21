from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

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


# Filenames that often hold DB credentials (basename match, any depth).
_CONFIG_BASENAMES = {
    "wp-config.php",
    "configuration.php",
    "config.php",
    "db.php",
    "database.php",
    "connection.php",
    "settings.php",
    "localsettings.php",
    "env.php",
    ".env",
    ".env.local",
    ".env.production",
    ".env_local",
}

# Prefer these over *_old / backup copies when ranking hits.
_PREFERRED_BASENAMES = {
    "wp-config.php",
    "configuration.php",
    "config.php",
    ".env",
    "database.php",
    "db.php",
}

_WEB_PREFIXES = (
    "",
    "public_html/",
    "www/",
    "httpdocs/",
    "htdocs/",
    "web/",
    "public/",
    "site/",
)

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


def default_mysql_config_candidates() -> list[str]:
    """Static relative paths to try first (fast path before deep scan)."""
    leaves = [
        "wp-config.php",
        "configuration.php",
        ".env",
        ".env.local",
        ".env.production",
        "includes/config.php",
        "include/config.php",
        "inc/config.php",
        "config/config.php",
        "config.php",
        "db.php",
        "database.php",
        "connection.php",
        "settings.php",
        "app/config/database.php",
        "application/config/database.php",
        "app/etc/env.php",
        "sites/default/settings.php",
        "typo3conf/LocalConfiguration.php",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for prefix in _WEB_PREFIXES:
        for leaf in leaves:
            rel = f"{prefix}{leaf}".replace("//", "/").lstrip("/")
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def expand_mysql_config_candidates(candidates: list[str] | None) -> list[str]:
    """Merge caller list with the full default set (deduped, defaults first)."""
    base = default_mysql_config_candidates()
    extra = [c.strip().lstrip("/") for c in (candidates or []) if c and c.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for rel in [*base, *extra]:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _is_interesting_config_name(name: str) -> bool:
    lower = name.lower()
    if lower in _CONFIG_BASENAMES:
        return True
    if lower.startswith(".env"):
        return True
    # include/config.php style already covered by basename; also *database*.php
    if lower.endswith(".php") and any(
        tok in lower for tok in ("config", "database", "dbconn", "connection")
    ):
        # Skip obvious noise
        if any(tok in lower for tok in ("config_old", ".bak", "sample", "example", "dist")):
            return False
        if lower in {"mysql.class.php", "mysqli.php", "pdo.php"}:
            return False
        return lower.endswith("config.php") or "database" in lower or lower in {
            "db.php",
            "connection.php",
            "settings.php",
        }
    return False


def _rank_config_path(path: str) -> tuple[int, int, str]:
    """Lower is better: prefer live config.php over config_old, prefer shallower paths."""
    name = Path(path.replace("\\", "/")).name.lower()
    penalty = 0
    if "old" in name or "bak" in name or "backup" in name or "sample" in name:
        penalty += 50
    if name not in _PREFERRED_BASENAMES and not name.startswith(".env"):
        penalty += 10
    depth = path.replace("\\", "/").count("/")
    return (penalty, depth, path.lower())


def find_local_mysql_config_files(files_root: Path, *, max_files: int = 80) -> list[Path]:
    """Walk a local day ``files/`` tree for likely DB config files."""
    if not files_root.is_dir():
        return []
    hits: list[Path] = []
    for path in files_root.rglob("*"):
        if not path.is_file():
            continue
        if path.stat().st_size > 1_500_000:
            continue
        if not _is_interesting_config_name(path.name):
            continue
        hits.append(path)
        if len(hits) >= max_files * 3:
            break
    hits.sort(key=lambda p: _rank_config_path(str(p.relative_to(files_root).as_posix())))
    return hits[:max_files]


def find_remote_mysql_config_paths(
    client: RemoteClient,
    remote_root: str,
    *,
    max_files: int = 60,
    max_folders: int = 400,
) -> list[str]:
    """
    Limited BFS under remote_root for config-like filenames.

    Used when the static candidate list misses nested apps
    (e.g. ``/public_html/portal_…/include/config.php``).
    """
    walk = getattr(client, "walk_folders", None)
    hits: list[str] = []
    if walk is None:
        return hits
    folders = 0
    try:
        for _folder, files in walk(remote_root):
            folders += 1
            for entry in files:
                name = entry.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                if _is_interesting_config_name(name):
                    hits.append(entry.path)
            if folders >= max_folders or len(hits) >= max_files:
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("Remote config scan stopped early: %s", exc)
    hits.sort(key=_rank_config_path)
    return hits[:max_files]


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


def _parse_text(text: str, source_path: str, profile: str) -> MysqlCredentials | None:
    creds = parse_php_array_profile(text, profile, source_path)
    if creds is None:
        creds = parse_mysql_credentials(text, source_path)
    return creds


def discover_from_local_files(
    files_root: Path,
    *,
    profile: str = "web",
    max_files: int = 80,
) -> list[MysqlCredentials]:
    """Parse DB credentials from already-downloaded site files (preferred after sync)."""
    found: list[MysqlCredentials] = []
    seen: set[tuple[str, str, str, int]] = set()
    for path in find_local_mysql_config_files(files_root, max_files=max_files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Could not read local config %s: %s", path, exc)
            continue
        source = str(path)
        creds = _parse_text(text, source, profile)
        if creds is None:
            log.debug("Local config %s has no database credentials", path)
            continue
        key = (creds.host, creds.user, creds.database, creds.port)
        if key in seen:
            continue
        seen.add(key)
        log.info(
            "MySQL config found locally in %s (db=%s host=%s user=%s)",
            path,
            creds.database,
            creds.host,
            creds.user,
        )
        found.append(creds)
    return found


def discover_all_mysql_credentials(
    client: RemoteClient | None,
    remote_root: str,
    candidates: list[str] | None = None,
    *,
    profile: str = "web",
    deep_scan: bool = True,
    local_files_dir: Path | None = None,
) -> list[MysqlCredentials]:
    """
    Find MySQL credentials from site config files.

    Order:
    1. Already-downloaded local ``files/`` tree (fast, nested paths).
    2. Expanded static candidate paths over FTP/SFTP.
    3. Optional limited remote BFS for config-like filenames.
    """
    found: list[MysqlCredentials] = []
    seen: set[tuple[str, str, str, int]] = set()

    def _add(creds: MysqlCredentials) -> None:
        key = (creds.host, creds.user, creds.database, creds.port)
        if key in seen:
            return
        seen.add(key)
        found.append(creds)

    if local_files_dir is not None:
        for creds in discover_from_local_files(local_files_dir, profile=profile):
            _add(creds)
        if found:
            return found

    if client is None:
        return found

    paths = expand_mysql_config_candidates(candidates)
    for rel in paths:
        rel = rel.lstrip("/")
        remote_path = f"{remote_root.rstrip('/')}/{rel}" if remote_root != "/" else f"/{rel}"
        try:
            if not client.exists(remote_path):
                continue
            text = client.read_bytes(remote_path).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", remote_path, exc)
            continue
        creds = _parse_text(text, remote_path, profile)
        if creds is None:
            log.info("Config %s has no database credentials", remote_path)
            continue
        log.info(
            "MySQL config found in %s (db=%s host=%s user=%s)",
            remote_path,
            creds.database,
            creds.host,
            creds.user,
        )
        _add(creds)

    if found or not deep_scan:
        return found

    log.info(
        "Static DB config candidates missed — deep-scanning remote tree under %s",
        remote_root,
    )
    for remote_path in find_remote_mysql_config_paths(client, remote_root):
        try:
            text = client.read_bytes(remote_path).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", remote_path, exc)
            continue
        creds = _parse_text(text, remote_path, profile)
        if creds is None:
            continue
        log.info(
            "MySQL config found via deep scan in %s (db=%s host=%s user=%s)",
            remote_path,
            creds.database,
            creds.host,
            creds.user,
        )
        _add(creds)
    return found


def discover_mysql_credentials(
    client: RemoteClient | None,
    remote_root: str,
    candidates: list[str] | None = None,
    *,
    profile: str = "web",
    local_files_dir: Path | None = None,
) -> MysqlCredentials:
    """
    Scan common CMS/app config paths (and nested trees) for MySQL credentials.

    Supports WordPress-style define(), Joomla public $props, .env keys, classic
    ``$db_host`` / ``$databse_name`` assigns, and PHP array profiles.
    """
    found = discover_all_mysql_credentials(
        client,
        remote_root,
        candidates,
        profile=profile,
        local_files_dir=local_files_dir,
    )
    if found:
        return found[0]
    raise FileNotFoundError(
        "Could not discover MySQL credentials from website config files "
        "(checked common paths, public_html/www prefixes, and a deep filename scan)."
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
