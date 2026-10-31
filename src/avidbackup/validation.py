from __future__ import annotations

import re

ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_account_id(account_id: str) -> str:
    aid = (account_id or "").strip()
    if not aid or not ACCOUNT_ID_RE.match(aid):
        raise ValueError(
            f"Invalid account id {account_id!r}. "
            "Use only letters, digits, dot, underscore, hyphen (^[A-Za-z0-9._-]+$)."
        )
    if ".." in aid or aid.startswith(".") or "/" in aid or "\\" in aid:
        raise ValueError(f"Invalid account id {account_id!r}: path traversal rejected")
    return aid
