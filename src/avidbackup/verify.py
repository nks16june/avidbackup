from __future__ import annotations

import logging
from pathlib import Path

from avidbackup.databases.compress import verify_sha256
from avidbackup.manifest import Manifest
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import AppConfig, load_config

log = logging.getLogger(__name__)


def verify_account(cfg: AppConfig, account_id: str) -> int:
    account = cfg.account(account_id)
    settings = settings_from_account(cfg, account)
    errors = 0

    # Manifest vs local files
    man = Manifest(settings.manifest_path)
    man.load()
    missing = 0
    size_mismatch = 0
    for _remote_path, state in (man.entries or {}).items():
        local = Path(state.local_path) if state.local_path else None
        if local is None or not local.is_file():
            missing += 1
            continue
        if state.size and local.stat().st_size != state.size:
            size_mismatch += 1
    if missing or size_mismatch:
        print(f"[{account_id}] files: missing={missing} size_mismatch={size_mismatch}")
        errors += 1
    else:
        print(f"[{account_id}] files: OK ({len(man.entries)} manifest entries)")

    # Newest dump per pattern
    mysql_dir = settings.local_mysql_dir
    dumps = []
    if mysql_dir.is_dir():
        for pattern in ("*.sql", "*.sql.gz", "*.dump", "*.dump.gz", "*.sqlite", "*.sqlite.gz"):
            dumps.extend(mysql_dir.glob(pattern))
    dumps = [d for d in dumps if d.is_file()]
    if not dumps:
        print(f"[{account_id}] dumps: none found (ok if DB backup not configured)")
    else:
        newest = max(dumps, key=lambda p: p.stat().st_mtime)
        if newest.stat().st_size <= 0:
            print(f"[{account_id}] dumps: FAIL empty {newest.name}")
            errors += 1
        elif not verify_sha256(newest):
            print(f"[{account_id}] dumps: FAIL checksum {newest.name}")
            errors += 1
        else:
            ok_marker = True
            if newest.suffix in {".sql"} or newest.name.endswith(".sql.gz"):
                # basic completeness for plain SQL
                try:
                    if newest.name.endswith(".gz"):
                        import gzip

                        with gzip.open(newest, "rt", encoding="utf-8", errors="replace") as fh:
                            tail = fh.read()[-2000:]
                    else:
                        with newest.open("r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(0, 2)
                            size = fh.tell()
                            fh.seek(max(0, size - 2000))
                            tail = fh.read()
                    ok_marker = "Dump completed" in tail or "dump complete" in tail.lower()
                except OSError:
                    ok_marker = False
            if not ok_marker:
                print(f"[{account_id}] dumps: WARN completeness marker missing in {newest.name}")
            print(f"[{account_id}] dumps: OK newest={newest.name} bytes={newest.stat().st_size}")
    return 1 if errors else 0


def verify_all(account_ids: list[str] | None = None) -> int:
    cfg = load_config()
    ids = account_ids or [a.id for a in cfg.enabled_accounts()]
    if not ids:
        print("No accounts to verify")
        return 2
    worst = 0
    for aid in ids:
        try:
            worst = max(worst, verify_account(cfg, aid))
        except Exception as exc:  # noqa: BLE001
            print(f"[{aid}] verify error: {exc}")
            worst = 1
    return worst
