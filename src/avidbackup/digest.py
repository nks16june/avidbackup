"""One daily status covering every account: done, failed, or not run."""

from __future__ import annotations

from avidbackup.backup_report import build_report
from avidbackup.store import AppConfig

_LABELS = {
    "ok": "OK",
    "warn": "OK, with a warning",
    "fail": "FAILED",
    "missed": "NOT RUN",
    "running": "STILL RUNNING",
    "na": "not due yet",
}

# Dashboard cell "fail" covers failed + interrupted runs.


def build_digest(cfg: AppConfig, account_ids: list[str] | None = None) -> tuple[str, str, bool]:
    """Return (title, message, urgent). Urgent when any included account is not backed up today."""
    report = build_report(cfg, days=1)
    if account_ids:
        wanted = set(account_ids)
        report.accounts = [a for a in report.accounts if a.account_id in wanted]
    urgent = False
    lines: list[str] = [f"AvidBackup {report.today.strftime('%d %b %Y')}"]
    for account in report.accounts:
        status = account.cells.get(report.today, "na")
        if status in {"fail", "missed", "running"}:
            urgent = True
        word = _LABELS.get(status, status)
        extra = ""
        today_runs = [
            run
            for row in report.day_rows
            if row.day == report.today
            for run in row.runs
            if run.account_id == account.account_id
        ]
        if today_runs:
            last = today_runs[0]
            extra = f" — files {last.files_new}, db {last.dumps}"
            if last.error and status != "ok":
                extra += f" — {last.error}"
        lines.append(f"{account.label}: {word}{extra}")
    lines.append(
        f"Restore one day from {cfg.data_root}\\<account>\\{report.today.isoformat()}\\files or \\db"
    )
    if not report.accounts:
        urgent = True
        lines.append("No accounts are configured.")
    title = "AvidBackup problem" if urgent else "AvidBackup OK"
    return title, "\n".join(lines), urgent
