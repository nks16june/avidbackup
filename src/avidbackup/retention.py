from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DUMP_GLOBS = ("*.sql", "*.sql.gz", "*.dump", "*.dump.gz", "*.sqlite", "*.sqlite.gz")


def prune_mysql_dumps(mysql_dir: Path, keep_days: int = 15) -> int:
    """
    Keep database dumps for keep_days.
    Site files under files/ are never pruned by this function.
    """
    if keep_days <= 0 or not mysql_dir.is_dir():
        return 0
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = 0
    seen: set[Path] = set()
    for pattern in DUMP_GLOBS:
        for path in mysql_dir.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                    side = Path(str(path) + ".sha256")
                    side.unlink(missing_ok=True)
                    Path(str(path) + ".done").unlink(missing_ok=True)
                    log.info("Pruned old dump (> %s days): %s", keep_days, path.name)
                except OSError as exc:
                    log.warning("Could not prune %s: %s", path, exc)
    return removed
