"""Day-wise view of what has been backed up, built from per-account history and dump files.

Read-only: it never creates or modifies history databases, manifests or dumps.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from avidbackup.retention import DUMP_GLOBS
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import AppConfig

STALE_RUNNING_HOURS = 12
# Worst first: used to roll several accounts up into one status for a day.
_ROLLUP_ORDER = ("fail", "missed", "warn", "running", "ok")


@dataclass
class RunRow:
    account_id: str
    label: str
    started: datetime
    duration_s: int | None
    status: str  # success | partial | failed | running | interrupted
    files_new: int
    files_skipped: int
    files_failed: int
    files_bytes: int
    dumps: int
    dump_bytes: int
    error: str | None


@dataclass
class DumpRow:
    account_id: str
    label: str
    name: str
    size: int
    created: datetime
    checksum: bool
    kept_until: date | None


@dataclass
class DayRow:
    day: date
    status: str  # ok | warn | fail | missed | running | na
    runs: list[RunRow] = field(default_factory=list)
    dumps: list[DumpRow] = field(default_factory=list)
    account_status: dict[str, str] = field(default_factory=dict)  # account id -> status
    account_labels: dict[str, str] = field(default_factory=dict)  # account id -> label

    def accounts_with(self, status: str) -> list[str]:
        return [
            self.account_labels.get(aid, aid)
            for aid, st in self.account_status.items()
            if st == status
        ]

    @property
    def new_files(self) -> int:
        return sum(r.files_new for r in self.runs)

    @property
    def bytes_total(self) -> int:
        return sum(r.files_bytes + r.dump_bytes for r in self.runs)


@dataclass
class AccountReport:
    account_id: str
    label: str
    first_day: date | None
    cells: dict[date, str]
    files_cells: dict[date, str] = field(default_factory=dict)
    db_cells: dict[date, str] = field(default_factory=dict)
    files_updated_by_day: dict[date, int] = field(default_factory=dict)
    last_success: datetime | None = None
    last_files_ok: datetime | None = None
    last_db_ok: datetime | None = None
    files_tracked: int = 0
    files_bytes: int = 0
    dumps_count: int = 0
    dumps_bytes: int = 0


@dataclass
class Report:
    days: list[date]  # oldest -> newest
    accounts: list[AccountReport]
    day_rows: list[DayRow]  # newest -> oldest
    last_success: datetime | None
    covered_days: int
    expected_days: int
    failed_runs: int
    total_bytes: int
    total_files_bytes: int
    total_db_bytes: int
    today: date

    @property
    def coverage_pct(self) -> int | None:
        if not self.expected_days:
            return None
        return round(100 * self.covered_days / self.expected_days)


def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_runs(db_path: Path, since_utc: datetime) -> list[dict[str, Any]]:
    """Read runs without creating or migrating the history database."""
    if not db_path.is_file():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM runs WHERE started_at >= ? AND status != 'dry-run' ORDER BY started_at",
            (since_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _first_run_day(db_path: Path, tz: tzinfo | None) -> date | None:
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MIN(started_at) FROM runs WHERE status != 'dry-run'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    started = _parse_utc(row[0] if row else None)
    return started.astimezone(tz).date() if started else None


def _is_checksum_sidecar(path: Path) -> bool:
    return path.name.endswith((".sha256", ".done"))


def list_dump_files(mysql_dir: Path) -> list[Path]:
    from avidbackup.daystore import dump_directories

    roots: list[Path] = []
    if mysql_dir.name in {"mysql", "db"}:
        account_root = mysql_dir.parent if mysql_dir.name == "mysql" else mysql_dir.parent.parent
        roots.extend(dump_directories(account_root))
    if mysql_dir not in roots:
        roots.append(mysql_dir)
    seen: dict[tuple[int, int], Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in DUMP_GLOBS:
            for p in root.glob(pattern):
                if not p.is_file() or p.name.startswith(".") or _is_checksum_sidecar(p):
                    continue
                st = p.stat()
                seen[(st.st_dev, st.st_ino)] = p
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _manifest_totals(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    try:
        files = json.loads(path.read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError):
        return 0, 0
    count = 0
    total = 0
    for entry in files.values():
        if isinstance(entry, dict):
            count += 1
            total += int(entry.get("size", 0) or 0)
    return count, total


def _day_status(runs: list[RunRow], *, before_first_run: bool) -> str:
    if not runs:
        return "na" if before_first_run else "missed"
    last = runs[-1]
    if last.status == "success":
        return "ok"
    if last.status == "partial":
        return "warn"
    if last.status == "running":
        return "running"
    if any(r.status == "success" for r in runs):
        return "warn"
    if any(r.status == "partial" for r in runs):
        return "warn"
    return "fail"


def _files_day_status(runs: list[RunRow], *, before_first_run: bool) -> str:
    """Status for website-file backup only."""
    if not runs:
        return "na" if before_first_run else "missed"
    if any(r.status == "running" for r in runs):
        return "running"
    # partial = files OK, DB not — still counts as files OK for the files row.
    ok_runs = [
        r
        for r in runs
        if r.status in {"success", "partial"} and r.files_failed == 0
    ]
    if ok_runs:
        if any(r.status in {"failed", "interrupted"} or r.files_failed for r in runs):
            return "warn"
        return "ok"
    if any(r.files_failed or r.status in {"failed", "interrupted"} for r in runs):
        return "fail"
    if any(r.status in {"success", "partial"} for r in runs):
        return "ok"
    return "fail"


def _db_day_status(
    runs: list[RunRow],
    dumps: list[DumpRow],
    *,
    before_first_run: bool,
) -> str:
    """
    Status for database dumps only.

    ``ok`` means a dump file is still on disk for this day (restoreable).
    History alone is not enough — pruned dumps must not look like restore points.
    """
    if dumps:
        if any(r.status in {"failed", "interrupted", "partial"} and r.dumps == 0 for r in runs):
            return "warn"
        return "ok"
    if not runs:
        return "na" if before_first_run else "missed"
    if any(r.status == "running" for r in runs):
        return "running"
    # History said a dump was taken but the file is gone (retention / move).
    if any(r.dumps > 0 and r.status in {"success", "failed", "partial"} for r in runs):
        return "missed"
    # Ran with intentional DB attempt but no dump on disk (partial / failed discover).
    if any(r.status == "partial" for r in runs):
        return "fail"
    if any(r.status == "success" for r in runs):
        return "missed"
    return "fail"


def _rollup(statuses: list[str]) -> str:
    real = [s for s in statuses if s != "na"]
    if not real:
        return "na"
    for candidate in _ROLLUP_ORDER:
        if candidate in real:
            return candidate
    return real[0]


def build_report(
    cfg: AppConfig,
    *,
    days: int = 30,
    account_id: str | None = None,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> Report:
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    since_utc = datetime.combine(day_list[0], datetime.min.time(), tzinfo=now.tzinfo).astimezone(
        timezone.utc
    )

    accounts = [a for a in cfg.accounts if account_id in (None, "", a.id)]
    acc_reports: list[AccountReport] = []
    runs_by_day: dict[date, list[RunRow]] = {d: [] for d in day_list}
    dumps_by_day: dict[date, list[DumpRow]] = {d: [] for d in day_list}
    per_account_cells: list[dict[date, str]] = []
    last_success: datetime | None = None
    failed_runs = 0
    total_bytes = 0
    total_files_bytes = 0
    total_db_bytes = 0

    for acct in accounts:
        try:
            settings = settings_from_account(cfg, acct)
        except Exception:  # noqa: BLE001
            continue
        raw = _read_runs(settings.history_db, since_utc)
        first_day = _first_run_day(settings.history_db, tz)
        retention_days = int(getattr(acct.retention, "mysql_days", 0) or 0)

        acct_runs: dict[date, list[RunRow]] = {d: [] for d in day_list}
        acct_last_success: datetime | None = None
        for r in raw:
            started = _parse_utc(r["started_at"])
            if started is None:
                continue
            started = started.astimezone(tz)
            finished = _parse_utc(r.get("finished_at"))
            status = r["status"]
            error = r.get("error")
            if status == "running":
                from avidbackup.history import interrupt_reason
                from avidbackup.locks import pid_alive

                detail = {}
                try:
                    detail = json.loads(r.get("detail_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    detail = {}
                pid = detail.get("pid")
                try:
                    pid_i = int(pid) if pid is not None else None
                except (TypeError, ValueError):
                    pid_i = None
                dead = pid_i is not None and not pid_alive(pid_i)
                too_old = (now - started) > timedelta(hours=STALE_RUNNING_HOURS)
                if dead or too_old:
                    status = "interrupted"
                    error = error or interrupt_reason(
                        pid=pid_i if dead else None,
                        account_id=acct.id,
                        hours=None if dead else STALE_RUNNING_HOURS,
                    )
            duration = int((finished - started.astimezone(finished.tzinfo)).total_seconds()) if finished else None
            row = RunRow(
                account_id=acct.id,
                label=acct.label,
                started=started,
                duration_s=duration,
                status=status,
                files_new=int(r.get("files_downloaded") or 0),
                files_skipped=int(r.get("files_skipped") or 0),
                files_failed=int(r.get("files_failed") or 0),
                files_bytes=int(r.get("files_bytes") or 0),
                dumps=int(r.get("mysql_downloaded") or 0),
                dump_bytes=int(r.get("mysql_bytes") or 0),
                error=error,
            )
            d = started.date()
            if d in acct_runs:
                acct_runs[d].append(row)
                runs_by_day[d].append(row)
            if status in {"failed", "interrupted"}:
                failed_runs += 1
            # Partial counts as files progress for "last success" date, but UI
            # must still show Partial — tip comes from run.error.
            if status in {"success", "partial"} and (
                acct_last_success is None or started > acct_last_success
            ):
                acct_last_success = started

        cells: dict[date, str] = {}
        files_cells: dict[date, str] = {}
        db_cells: dict[date, str] = {}
        files_updated_by_day: dict[date, int] = {}
        last_files_ok: datetime | None = None
        last_db_ok: datetime | None = None
        for d in day_list:
            before_first = first_day is None or d < first_day
            day_runs = acct_runs[d]
            cells[d] = _day_status(day_runs, before_first_run=before_first)
            files_cells[d] = _files_day_status(day_runs, before_first_run=before_first)
            files_updated_by_day[d] = sum(r.files_new for r in day_runs)
            for r in day_runs:
                if r.status in {"success", "partial"} and r.files_failed == 0:
                    if last_files_ok is None or r.started > last_files_ok:
                        last_files_ok = r.started
        per_account_cells.append(cells)

        from avidbackup.day_browser import dump_day_key

        dump_paths = list_dump_files(settings.local_mysql_dir)
        dumps_bytes = 0
        for p in dump_paths:
            st = p.stat()
            dumps_bytes += st.st_size
            created = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone(tz)
            # Prefer filename _YYYYMMDD_ so calendar day matches restore day browser.
            stamp = dump_day_key(p)
            try:
                dump_day = date.fromisoformat(stamp) if stamp else created.date()
            except ValueError:
                dump_day = created.date()
            if dump_day in dumps_by_day:
                dumps_by_day[dump_day].append(
                    DumpRow(
                        account_id=acct.id,
                        label=acct.label,
                        name=p.name,
                        size=st.st_size,
                        created=created,
                        checksum=Path(str(p) + ".sha256").is_file(),
                        kept_until=(dump_day + timedelta(days=retention_days))
                        if retention_days > 0
                        else None,
                    )
                )
                if last_db_ok is None or created > last_db_ok:
                    last_db_ok = created
        for d in day_list:
            before_first = first_day is None or d < first_day
            db_cells[d] = _db_day_status(
                acct_runs[d],
                [x for x in dumps_by_day[d] if x.account_id == acct.id],
                before_first_run=before_first,
            )
        files_count, files_bytes = _manifest_totals(settings.manifest_path)
        total_bytes += files_bytes + dumps_bytes
        total_files_bytes += files_bytes
        total_db_bytes += dumps_bytes
        if acct_last_success and (last_success is None or acct_last_success > last_success):
            last_success = acct_last_success
        acc_reports.append(
            AccountReport(
                account_id=acct.id,
                label=acct.label,
                first_day=first_day,
                cells=cells,
                files_cells=files_cells,
                db_cells=db_cells,
                files_updated_by_day=files_updated_by_day,
                last_success=acct_last_success,
                last_files_ok=last_files_ok,
                last_db_ok=last_db_ok,
                files_tracked=files_count,
                files_bytes=files_bytes,
                dumps_count=len(dump_paths),
                dumps_bytes=dumps_bytes,
            )
        )

    expected = 0
    covered = 0
    day_rows: list[DayRow] = []
    for d in reversed(day_list):
        statuses = [cells[d] for cells in per_account_cells]
        by_account = {
            acc.account_id: acc.cells[d] for acc in acc_reports if acc.cells[d] != "na"
        }
        for s in statuses:
            if s != "na":
                expected += 1
                if s in ("ok", "warn"):
                    covered += 1
        day_rows.append(
            DayRow(
                day=d,
                status=_rollup(statuses),
                account_status=by_account,
                account_labels={acc.account_id: acc.label for acc in acc_reports},
                runs=sorted(runs_by_day[d], key=lambda r: r.started, reverse=True),
                dumps=sorted(dumps_by_day[d], key=lambda x: x.created, reverse=True),
            )
        )

    return Report(
        days=day_list,
        accounts=acc_reports,
        day_rows=day_rows,
        last_success=last_success,
        covered_days=covered,
        expected_days=expected,
        failed_runs=failed_runs,
        total_bytes=total_bytes,
        total_files_bytes=total_files_bytes,
        total_db_bytes=total_db_bytes,
        today=today,
    )
