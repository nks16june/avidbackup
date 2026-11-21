"""Append-only audit trail for sensitive actions (restores, failed sign-ins, tool installs).

One JSON object per line in logs/audit.log. Never pass secrets; callers add only ids,
counts and paths.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from avidbackup.paths import audit_log_path

log = logging.getLogger(__name__)
MAX_BYTES = 5 * 1024 * 1024


def write_audit(action: str, **fields: Any) -> None:
    entry = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": action}
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # never break the action because logging failed
        log.warning("Could not write audit log: %s", exc)
