from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from avidbackup.audit import write_audit
from avidbackup.backup_report import build_report, human_bytes
from avidbackup.day_browser import (
    list_directory,
    list_restore_days,
    parse_day,
    resolve_download,
    snapshot_for_day,
)
from avidbackup.daystore import day_stamp
from avidbackup.initial_full import has_completed_initial_full
from avidbackup.live_status import build_live_status
from avidbackup.log_tail import read_log_chunk
from avidbackup.paths import app_root, data_root, ensure_layout
from avidbackup.pgtools import PgToolsError, install_postgres_tools
from avidbackup.procutil import spawn_account_runs, spawn_cli
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import (
    AccountConfig,
    AppConfig,
    DatabaseConfig,
    FtpConfig,
    RetentionConfig,
    delete_account,
    load_config,
    new_account_id,
    resolve_data_dir,
    save_config,
    upsert_account,
)
from avidbackup.util_bin import find_pg_dump, find_psql
from avidbackup.validation import validate_account_id
from avidbackup.webui.security import (
    is_loopback_host,
    load_or_create_secret_key,
    register_security,
    require_ui_token_match,
    resolve_ui_token,
    safe_next,
    token_fingerprint,
)

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent / "templates"
RESTORE_PLAN_SECONDS = 600

# Single Sites status model (Protected / Partial / Failed / Running + setup states).
_STATUS_META = {
    "ok": ("✓", "Protected"),
    "warn": ("!", "Partial"),
    "fail": ("✕", "Failed"),
    "missed": ("○", "Missed"),
    "running": ("…", "Running"),
    "na": ("–", "No history"),
    "needs_full": ("◆", "Needs full"),
}


def _discovered_session_key(account_id: str) -> str:
    return f"discovered_db:{account_id or '__new__'}"


def _store_discovered_creds(account_id: str, rows: list[dict]) -> None:
    """Keep full DB secrets server-side only (never echo password in JSON)."""
    session[_discovered_session_key(account_id)] = rows


def _pop_discovered_password(
    account_id: str,
    *,
    database: str,
    user: str,
    host: str,
) -> str:
    stored = session.get(_discovered_session_key(account_id)) or []
    for row in stored:
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("database") or "") == database
            and str(row.get("user") or "") == user
            and (not host or str(row.get("host") or "") == host)
        ):
            return str(row.get("password") or "")
    return ""


def _last_db_failure_tip(cfg: AppConfig, account_id: str, day: str) -> str | None:
    """Human tip when restore DB tab has no dumps for this day."""
    try:
        account = cfg.account(account_id)
        settings = settings_from_account(cfg, account)
        from avidbackup.history import HistoryStore

        hist = HistoryStore(settings.history_db)
        for run in hist.list_runs(limit=80):
            started = str(run.get("started_at") or "")
            if not started.startswith(day):
                continue
            status = str(run.get("status") or "")
            dumps = int(run.get("mysql_downloaded") or 0)
            err = (run.get("error") or "").strip()
            if status in {"partial", "failed"} and dumps == 0:
                tip = err or "Database dump failed for this day."
                if "Access denied" in tip or "1045" in tip:
                    tip += (
                        " Allowlist this PC’s public IP under cPanel → Remote MySQL, "
                        "then Resume backup."
                    )
                return tip
            if status == "partial" and err:
                return err
    except Exception:  # noqa: BLE001
        log.exception("Could not load DB failure tip for %s %s", account_id, day)
    return None


def _cfg_data_root(cfg: AppConfig) -> Path:
    return Path(cfg.data_root).expanduser().resolve() if cfg.data_root else data_root().resolve()


def _path_under_root(candidate: Path, root: Path) -> Path | None:
    """Resolve candidate and require it to stay under root (no traversal / symlink escape)."""
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def _today_health(cfg: AppConfig) -> dict[str, dict[str, str | None]]:
    """Map account_id -> {status, label, icon, tip} from the day report (1-day window)."""
    report = build_report(cfg, days=1)
    out: dict[str, dict[str, str | None]] = {}
    for acc in report.accounts:
        st = acc.cells.get(report.today, "na")
        icon, label = _STATUS_META.get(st, _STATUS_META["na"])
        out[acc.account_id] = {"status": st, "label": label, "icon": icon, "tip": None}

    if report.day_rows:
        seen: set[str] = set()
        for run in report.day_rows[0].runs:
            if run.account_id in seen:
                continue
            seen.add(run.account_id)
            info = out.get(run.account_id)
            if not info:
                continue
            tip_parts: list[str] = []
            if run.status == "interrupted":
                tip_parts.append(run.error or "Interrupted before finish")
            elif run.status == "partial":
                tip_parts.append(run.error or "Files OK; database dump incomplete")
            elif run.error:
                tip_parts.append(run.error)
            elif run.status == "failed":
                tip_parts.append("Backup failed")
            elif run.status == "running":
                tip_parts.append("Backup in progress")
            info["tip"] = " · ".join(tip_parts) if tip_parts else None
            # Prefer last-run honesty over rollup when today is partial.
            if run.status == "partial" and info.get("status") == "ok":
                icon, label = _STATUS_META["warn"]
                info["status"] = "warn"
                info["label"] = label
                info["icon"] = icon
    return out


def _row_status(
    *,
    initial_done: bool,
    health: dict[str, str | None] | None,
) -> dict[str, str | None]:
    """Pick the badge to show: needs initial full when relevant, else today's report cell."""
    if health is None:
        health = {"status": "na", "label": _STATUS_META["na"][1], "icon": _STATUS_META["na"][0], "tip": None}
    st = str(health.get("status") or "na")
    if not initial_done and st in {"na", "missed"}:
        icon, label = _STATUS_META["needs_full"]
        return {
            "status": "needs_full",
            "label": label,
            "icon": icon,
            "tip": "Initial full backup has not completed yet",
        }
    return {
        "status": st,
        "label": str(health.get("label") or _STATUS_META.get(st, _STATUS_META["na"])[1]),
        "icon": str(health.get("icon") or _STATUS_META.get(st, _STATUS_META["na"])[0]),
        "tip": health.get("tip"),
    }


def create_app(*, bind_host: str | None = None) -> Flask:
    ensure_layout()
    cfg0 = load_config()
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.secret_key = load_or_create_secret_key()
    register_security(
        app,
        ui_token=resolve_ui_token(cfg0.ui_token),
        allow_nonloopback=bool(bind_host) and not is_loopback_host(bind_host or ""),
    )

    app.jinja_env.filters["human_bytes"] = human_bytes
    app.jinja_env.filters["human_int"] = lambda n: f"{int(n or 0):,}"

    @app.context_processor
    def inject_brand():
        import avidbackup

        return {
            "brand": "AvidBackup",
            "vendor": "Avid Web Solutions",
            "author": "Nishant Shrivastava",
            "home": str(app_root()),
            "app_version": avidbackup.__version__,
            "nav": request.endpoint,
        }

    @app.get("/login")
    def login():
        target = safe_next(request.args.get("next"), url_for("index"))
        expected = app.config.get("AVIDBACKUP_UI_TOKEN") or ""
        if not expected or (
            session.get("ui_authed") is True and session.get("ui_tok") == token_fingerprint(expected)
        ):
            return redirect(target)
        return render_template("login.html", next=target)

    @app.post("/login")
    def login_submit():
        expected = app.config.get("AVIDBACKUP_UI_TOKEN") or ""
        provided = request.form.get("token") or ""
        target = safe_next(request.form.get("next"), url_for("index"))
        limiter = app.config["AVIDBACKUP_LOGIN_LIMITER"]
        client = request.remote_addr or "unknown"
        if limiter.blocked(client):
            flash("Too many failed attempts. Wait a minute and try again.", "bad")
            return render_template("login.html", next=target), 429
        if require_ui_token_match(provided, expected):
            limiter.reset(client)
            session.clear()  # fresh session after sign-in (no fixation)
            session["ui_authed"] = True
            session["ui_tok"] = token_fingerprint(expected)
            session.permanent = True
            return redirect(target)
        limiter.record_failure(client)
        write_audit("login_failed", remote=client)
        flash("Invalid UI token.", "bad")
        return render_template("login.html", next=target), 401

    @app.get("/")
    def index():
        cfg = load_config()
        overview = {
            "sites": 0,
            "ok": 0,
            "fail": 0,
            "running": 0,
            "missed": 0,
            "needs_full": 0,
            "last_success": None,
            "total_bytes": 0,
        }
        health_by_id: dict[str, dict[str, str | None]] = {}
        by_id: dict = {}
        try:
            health_by_id = _today_health(cfg)
            report30 = build_report(cfg, days=30)
            overview["last_success"] = report30.last_success
            overview["total_bytes"] = report30.total_bytes
            by_id = {acc.account_id: acc for acc in report30.accounts}
        except Exception:  # noqa: BLE001
            log.exception("Could not build backup health for accounts dashboard")
        rows = []
        for a in cfg.accounts:
            try:
                s = settings_from_account(cfg, a)
                initial = has_completed_initial_full(s)
                data_dir = str(s.local_backup_root)
            except Exception:  # noqa: BLE001
                initial = False
                data_dir = str(resolve_data_dir(cfg, a))
            status = _row_status(initial_done=initial, health=health_by_id.get(a.id))
            st = str(status.get("status") or "na")
            overview["sites"] += 1
            if st == "ok":
                overview["ok"] += 1
            elif st in {"fail", "warn"}:
                overview["fail"] += 1
            elif st == "running":
                overview["running"] += 1
            elif st == "missed":
                overview["missed"] += 1
            elif st == "needs_full":
                overview["needs_full"] += 1
            acc_rep = by_id.get(a.id)
            live = None
            try:
                live = build_live_status(cfg, a)
            except Exception:  # noqa: BLE001
                log.exception("Could not build live status for %s", a.id)
            # Prefer true live transfer over a stale "running" history cell.
            if live and live.get("live"):
                overview["running"] = overview.get("running", 0)
                if st != "running":
                    # recount: bump running, undo prior bucket if we miscounted
                    if st == "ok":
                        overview["ok"] = max(0, overview["ok"] - 1)
                    elif st in {"fail", "warn"}:
                        overview["fail"] = max(0, overview["fail"] - 1)
                    elif st == "missed":
                        overview["missed"] = max(0, overview["missed"] - 1)
                    elif st == "needs_full":
                        overview["needs_full"] = max(0, overview["needs_full"] - 1)
                    overview["running"] += 1
                status = {
                    "status": "running",
                    "label": "Live now",
                    "icon": "…",
                    "tip": live.get("message") or "Backup in progress",
                }
            elif st == "running" and live and not live.get("live"):
                # History says running but process/lock is gone — don't show as live.
                overview["running"] = max(0, overview["running"] - 1)
                overview["fail"] += 1
                status = {
                    "status": "fail",
                    "label": "Interrupted",
                    "icon": "✕",
                    "tip": (live.get("run") or {}).get("error") or "Backup process stopped",
                }
            rows.append(
                {
                    "account": a,
                    "initial_done": initial,
                    "data_dir": data_dir,
                    "db_count": len([d for d in a.databases if d.enabled]),
                    "status": status,
                    "files_bytes": getattr(acc_rep, "files_bytes", 0) or 0,
                    "dumps_bytes": getattr(acc_rep, "dumps_bytes", 0) or 0,
                    "last_success": getattr(acc_rep, "last_success", None),
                    "live": live,
                }
            )
        return render_template("index.html", cfg=cfg, rows=rows, overview=overview)

    @app.get("/backups")
    def backups():
        cfg = load_config()
        try:
            days = int(request.args.get("days", 30))
        except ValueError:
            days = 30
        days = days if days in (7, 30, 90) else 30
        account = (request.args.get("account") or "").strip()
        selected = None
        restore_points: list = []
        if account:
            try:
                validate_account_id(account)
                selected = cfg.account(account)
            except (KeyError, ValueError):
                flash("Unknown account.", "bad")
                return redirect(url_for("backups"))
            try:
                settings = settings_from_account(cfg, selected)
                restore_points = list_restore_days(settings.local_backup_root)
            except Exception:  # noqa: BLE001
                log.exception("Could not list restore days for %s", account)
        report = build_report(cfg, days=days, account_id=account or None)
        # Attach per-day file update counts + split status onto restore points.
        if selected and restore_points and report.accounts:
            acct_rep = report.accounts[0]
            for rp in restore_points:
                try:
                    from datetime import date as _date

                    d = _date.fromisoformat(rp.day)
                except ValueError:
                    continue
                rp.files_updated = int(acct_rep.files_updated_by_day.get(d, 0) or 0)
                rp.files_status = acct_rep.files_cells.get(d, "na")
                rp.db_status = acct_rep.db_cells.get(d, "na")
                if rp.dumps:
                    rp.db_status = "ok"
        return render_template(
            "backups.html",
            report=report,
            days=days,
            account=account,
            accounts=cfg.accounts,
            selected=selected,
            restore_points=restore_points,
        )

    def _load_account_or_redirect(account_id: str):
        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
            return cfg, account
        except (KeyError, ValueError):
            flash("Account not found or invalid id.", "bad")
            return None, None

    @app.get("/backups/<account_id>/<day>")
    def restore_day(account_id: str, day: str):
        cfg, account = _load_account_or_redirect(account_id)
        if account is None:
            return redirect(url_for("backups"))
        try:
            day = parse_day(day)
        except ValueError:
            flash("Invalid day.", "bad")
            return redirect(url_for("backups", account=account_id))

        tab = (request.args.get("tab") or "files").strip().lower()
        if tab not in {"files", "db"}:
            tab = "files"
        rel = (request.args.get("path") or "").strip()

        try:
            settings = settings_from_account(cfg, account)
            root = settings.local_backup_root
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not open account data: {exc}", "bad")
            return redirect(url_for("backups", account=account_id))

        snap = snapshot_for_day(root, day)
        entries = []
        crumbs: list[str] = []
        if tab == "files" and snap and snap.files_dir:
            try:
                rel_n, _abs, entries = list_directory(snap.files_dir, rel)
                rel = rel_n
                crumbs = rel.split("/") if rel else []
            except (ValueError, FileNotFoundError):
                flash("Folder not found in this snapshot.", "bad")
                return redirect(url_for("restore_day", account_id=account_id, day=day, tab="files"))

        if snap:
            try:
                from datetime import date as _date
                from datetime import datetime as _dt
                from datetime import time as _time

                from avidbackup.backup_report import build_report

                d = _date.fromisoformat(day)
                report = build_report(
                    cfg,
                    days=1,
                    account_id=account_id,
                    now=_dt.combine(d, _time(23, 59, 59)),
                )
                if report.accounts:
                    acct_rep = report.accounts[0]
                    snap.files_updated = int(acct_rep.files_updated_by_day.get(d, 0) or 0)
                    snap.files_status = acct_rep.files_cells.get(d, "na")
                    snap.db_status = acct_rep.db_cells.get(d, "na")
                    if snap.dumps:
                        snap.db_status = "ok"
            except Exception:  # noqa: BLE001
                log.exception("Could not enrich restore day status for %s %s", account_id, day)

        other_days = [s.day for s in list_restore_days(root)]
        day_path = str(root / day)
        db_empty_tip = None
        if tab == "db" and (not snap or not snap.dumps):
            db_empty_tip = _last_db_failure_tip(cfg, account_id, day)
        return render_template(
            "restore_day.html",
            account=account,
            day=day,
            tab=tab,
            rel=rel,
            crumbs=crumbs,
            entries=entries,
            snap=snap,
            other_days=other_days,
            day_path=day_path,
            days=30,
            db_empty_tip=db_empty_tip,
        )

    def _restore_back(account_id: str, day: str, rel: str | None = None):
        return redirect(
            url_for("restore_day", account_id=account_id, day=day, tab="files", path=rel or None)
        )

    @app.post("/backups/<account_id>/<day>/restore-ftp/preview")
    def restore_ftp_preview(account_id: str, day: str):
        """Step 1: show exactly what would be uploaded. Sends nothing to the server."""
        from avidbackup.restore_ftp import plan_restore

        cfg, account = _load_account_or_redirect(account_id)
        if account is None:
            return redirect(url_for("backups"))
        rel = (request.form.get("path") or "").strip()
        try:
            day = parse_day(day)
            settings = settings_from_account(cfg, account)
            snap = snapshot_for_day(settings.local_backup_root, day)
            if not snap or not snap.files_dir:
                raise FileNotFoundError("No files snapshot for this day")
            plan = plan_restore(snap.files_dir, rel=rel)
        except (ValueError, FileNotFoundError, KeyError, OSError) as exc:
            flash(f"Cannot prepare the restore: {exc}", "bad")
            return _restore_back(account_id, day, rel)
        if plan.files == 0:
            flash("Nothing to restore in that selection.", "bad")
            return _restore_back(account_id, day, rel)
        plan_id = secrets.token_urlsafe(16)
        session["restore_plan"] = {
            "id": plan_id,
            "account": account.id,
            "day": day,
            "rel": rel,
            "files": plan.files,
            "bytes": plan.bytes,
            "exp": time.time() + RESTORE_PLAN_SECONDS,
        }
        write_audit(
            "restore_ftp_previewed",
            account=account.id,
            day=day,
            path=rel or "/",
            files=plan.files,
            bytes=plan.bytes,
        )
        return render_template(
            "restore_preview.html",
            account=account,
            day=day,
            rel=rel,
            plan=plan,
            plan_id=plan_id,
            minutes=RESTORE_PLAN_SECONDS // 60,
        )

    @app.post("/backups/<account_id>/<day>/restore-ftp")
    def restore_ftp_day(account_id: str, day: str):
        """Step 2: upload this day's local files back to the server (needs a fresh preview)."""
        from avidbackup.restore_ftp import restore_day_files_via_ftp

        cfg, account = _load_account_or_redirect(account_id)
        if account is None:
            return redirect(url_for("backups"))
        rel = (request.form.get("path") or "").strip()
        try:
            day = parse_day(day)
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("backups", account=account_id))

        plan = session.get("restore_plan") or {}
        valid = (
            bool(plan.get("id"))
            and secrets.compare_digest(str(plan["id"]), request.form.get("plan_id") or "")
            and plan.get("account") == account.id
            and plan.get("day") == day
            and plan.get("rel") == rel
            and time.time() <= float(plan.get("exp") or 0)
        )
        if not valid:
            session.pop("restore_plan", None)
            flash("The restore preview expired or does not match. Preview the restore again.", "bad")
            return _restore_back(account_id, day, rel)
        if (request.form.get("confirm") or "").strip() != account.id:
            flash(f"Not restored: type the site ID ({account.id}) exactly to confirm.", "bad")
            return _restore_back(account_id, day, rel)
        session.pop("restore_plan", None)  # single use

        write_audit(
            "restore_ftp_started",
            account=account.id,
            day=day,
            path=rel or "/",
            files=plan.get("files"),
            host=account.ftp.host,
            remote=request.remote_addr,
        )
        try:
            settings = settings_from_account(cfg, account)
            snap = snapshot_for_day(settings.local_backup_root, day)
            if not snap or not snap.files_dir:
                raise FileNotFoundError("No files snapshot for this day")
            stats = restore_day_files_via_ftp(settings, snap.files_dir, rel=rel)
            write_audit(
                "restore_ftp_finished",
                account=account.id,
                day=day,
                uploaded=stats.uploaded,
                failed=stats.failed,
                bytes=stats.bytes,
            )
            if stats.failed:
                flash(
                    f"FTP restore finished with errors: {stats.uploaded} uploaded, "
                    f"{stats.failed} failed ({human_bytes(stats.bytes)}). "
                    f"First error: {(stats.errors or ['unknown'])[0]}",
                    "bad",
                )
            else:
                flash(
                    f"FTP restore OK: {stats.uploaded} file(s) "
                    f"({human_bytes(stats.bytes)}) pushed to {account.ftp.host} "
                    f"as {account.ftp.user}.",
                    "ok",
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("FTP restore failed for %s %s", account_id, day)
            write_audit("restore_ftp_failed", account=account.id, day=day, error=str(exc)[:200])
            flash(f"FTP restore failed: {exc}", "bad")
        return _restore_back(account_id, day, rel)

    @app.get("/backups/<account_id>/<day>/download")
    def download_restore(account_id: str, day: str):
        cfg, account = _load_account_or_redirect(account_id)
        if account is None:
            abort(404)
        kind = (request.args.get("kind") or "files").strip().lower()
        rel = (request.args.get("path") or "").strip()
        if kind not in {"files", "db"}:
            abort(400)
        try:
            day = parse_day(day)
            settings = settings_from_account(cfg, account)
            path = resolve_download(settings.local_backup_root, day, kind, rel)  # type: ignore[arg-type]
        except (ValueError, FileNotFoundError, KeyError):
            abort(404)
        # Ensure path stays under configured data root.
        data_r = _cfg_data_root(cfg)
        if _path_under_root(path, data_r) is None:
            abort(400)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.post("/backups/<account_id>/<day>/open-folder")
    def open_day_folder(account_id: str, day: str):
        cfg, account = _load_account_or_redirect(account_id)
        if account is None:
            return redirect(url_for("backups"))
        kind = (request.form.get("kind") or "files").strip().lower()
        rel = (request.form.get("path") or "").strip()
        try:
            day = parse_day(day)
            settings = settings_from_account(cfg, account)
            root = settings.local_backup_root
            day_dir = root / day
            if kind == "db":
                from avidbackup.day_browser import dumps_for_day

                day_db = day_dir / "db"
                legacy = root / "mysql"
                dumps = dumps_for_day(root, day)
                if dumps and dumps[0].get("path"):
                    target = Path(dumps[0]["path"]).parent
                elif day_db.is_dir():
                    target = day_db
                elif legacy.is_dir():
                    target = legacy
                else:
                    target = day_db
            else:
                files_root = day_dir / "files"
                from avidbackup.day_browser import normalize_rel, resolve_under

                rel_n = normalize_rel(rel)
                target = resolve_under(files_root, rel_n) if files_root.is_dir() else day_dir
                if target is None:
                    raise ValueError("Invalid path")
                if target.is_file():
                    target = target.parent
        except (ValueError, FileNotFoundError, KeyError) as exc:
            flash(str(exc), "bad")
            return redirect(url_for("backups", account=account_id))

        data_r = _cfg_data_root(cfg)
        safe = _path_under_root(target, data_r)
        if safe is None or not safe.is_dir():
            flash("Folder not found.", "bad")
            return redirect(url_for("restore_day", account_id=account_id, day=day, tab=kind if kind in {"files", "db"} else "files"))
        try:
            _open_in_file_manager(safe)
            flash(f"Opened {safe}", "ok")
        except OSError as exc:
            flash(f"Could not open folder: {exc}", "bad")
        return redirect(
            url_for(
                "restore_day",
                account_id=account_id,
                day=day,
                tab=kind if kind in {"files", "db"} else "files",
                path=rel or None,
            )
        )

    @app.get("/settings")
    def global_settings():
        from avidbackup.agent_service import get_agent_status
        from avidbackup.notify import ensure_phone_config, phone_subscribe_url

        cfg = ensure_phone_config(load_config())
        return render_template(
            "settings.html",
            cfg=cfg,
            phone_url=phone_subscribe_url(cfg),
            agent=get_agent_status(),
            pg_tools={
                "pg_dump": find_pg_dump(),
                "psql": find_psql(),
                "windows": sys.platform == "win32",
            },
        )

    @app.post("/agent/install")
    def install_agent():
        from avidbackup.agent_service import install_agent as _install_agent

        try:
            st = _install_agent(start_now=True)
            flash(
                f"Agent installed ({st.task_name}). "
                + ("Running now." if st.running else "Start it from Settings if needed."),
                "ok" if st.installed else "bad",
            )
        except OSError as exc:
            flash(f"Could not install agent: {exc}", "bad")
        return redirect(url_for("global_settings"))

    @app.post("/tools/install-postgres")
    def install_postgres():
        try:
            install_postgres_tools()
            flash("PostgreSQL tools installed.", "ok")
        except PgToolsError as exc:
            flash(f"Could not install PostgreSQL tools: {exc}", "bad")
        return redirect(url_for("global_settings"))

    @app.post("/settings")
    def save_global_settings():
        cfg = load_config()
        cfg.data_root = request.form.get("data_root", cfg.data_root).strip()
        cfg.mysql_retention_days = int(request.form.get("mysql_retention_days") or 15)
        cfg.files_keep_forever = request.form.get("files_keep_forever") == "on"
        cfg.large_table_mb = float(request.form.get("large_table_mb") or 50)
        cfg.download_retries = int(request.form.get("download_retries") or 3)
        cfg.compress_dumps = (request.form.get("compress_dumps") or "gzip").strip()
        cfg.ui_host = request.form.get("ui_host", cfg.ui_host).strip() or "127.0.0.1"
        cfg.ui_port = int(request.form.get("ui_port") or 8787)
        new_token = (request.form.get("ui_token") or "").strip()
        if new_token:
            cfg.ui_token = new_token
        # notify
        cfg.notify.webhook_url = (request.form.get("webhook_url") or "").strip()
        cfg.notify.webhook_secret = (request.form.get("webhook_secret") or "").strip() or cfg.notify.webhook_secret
        cfg.notify.smtp_host = (request.form.get("smtp_host") or "").strip()
        cfg.notify.smtp_port = int(request.form.get("smtp_port") or 587)
        cfg.notify.smtp_user = (request.form.get("smtp_user") or "").strip()
        smtp_pass = request.form.get("smtp_password") or ""
        if smtp_pass:
            cfg.notify.smtp_password = smtp_pass
        cfg.notify.smtp_from = (request.form.get("smtp_from") or "").strip()
        cfg.notify.smtp_to = (request.form.get("smtp_to") or "").strip()
        cfg.notify.phone_enabled = request.form.get("phone_enabled") == "on"
        cfg.notify.ntfy_details = request.form.get("ntfy_details") == "on"
        save_config(cfg)
        from avidbackup.notify import ensure_phone_config

        ensure_phone_config(cfg)
        flash("Global settings saved.", "ok")
        return redirect(url_for("global_settings"))

    @app.post("/settings/test-phone")
    def test_phone():
        from avidbackup.notify import channels_configured, ensure_phone_config, send_status

        cfg = load_config()
        cfg.notify.phone_enabled = request.form.get("phone_enabled") == "on"
        cfg.notify.ntfy_details = request.form.get("ntfy_details") == "on"
        save_config(cfg)
        cfg = ensure_phone_config(cfg)
        if not channels_configured(cfg):
            flash(
                "No notification channel is turned on. Tick the phone option (or add a webhook "
                "or email) and try again.",
                "bad",
            )
            return redirect(url_for("global_settings"))
        ok = send_status(
            cfg,
            "AvidBackup test",
            "Phone alerts are on. You will get a message after each backup and every morning.",
            urgent=False,
        )
        flash(
            "Test sent." if ok else "The test could not be delivered. See the log.",
            "ok" if ok else "bad",
        )
        return redirect(url_for("global_settings"))

    @app.get("/accounts/new")
    def new_account():
        return render_template("account_form.html", account=None, title="Add website account")

    @app.get("/accounts/<account_id>")
    def account_live(account_id: str):
        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
        except (KeyError, ValueError):
            flash("Account not found or invalid id.", "bad")
            return redirect(url_for("index"))
        try:
            live = build_live_status(cfg, account)
        except Exception:  # noqa: BLE001
            log.exception("Live status failed for %s", account_id)
            live = {
                "account_id": account_id,
                "label": account.label,
                "live": False,
                "phase": "idle",
                "message": "Could not read transfer status",
                "pct": 0,
                "samples": [],
                "recent": [],
                "data_dir": "",
            }
        return render_template("account_live.html", account=account, live=live)

    @app.get("/accounts/<account_id>/live.json")
    def account_live_json(account_id: str):
        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
        except (KeyError, ValueError):
            abort(404)
        try:
            return jsonify(build_live_status(cfg, account))
        except Exception:  # noqa: BLE001
            log.exception("Live JSON failed for %s", account_id)
            return jsonify({"account_id": account_id, "live": False, "phase": "idle", "error": "unavailable"}), 500

    @app.get("/accounts/<account_id>/log.json")
    def account_log_json(account_id: str):
        try:
            validate_account_id(account_id)
            load_config().account(account_id)
        except (KeyError, ValueError):
            abort(404)
        try:
            offset = int(request.args.get("offset") or 0)
        except ValueError:
            offset = 0
        offset = max(0, offset)
        try:
            return jsonify(read_log_chunk(account_id, offset=offset))
        except Exception:  # noqa: BLE001
            log.exception("Log JSON failed for %s", account_id)
            return jsonify({"account_id": account_id, "text": "", "offset": offset, "error": "unavailable"}), 500

    @app.get("/accounts/<account_id>/edit")
    def edit_account(account_id: str):
        try:
            validate_account_id(account_id)
            account = load_config().account(account_id)
        except (KeyError, ValueError):
            flash("Account not found or invalid id.", "bad")
            return redirect(url_for("index"))
        return render_template("account_form.html", account=account, title=f"Edit {account.label}")

    def _form_access_settings():
        """Build temporary Settings from the Add/Edit form access fields."""
        from avidbackup.config import Settings
        from avidbackup.paths import logs_dir
        from avidbackup.remote import normalize_access_protocol
        from avidbackup.secure_cli import normalize_password, resolve_secret

        account_id = (request.form.get("id") or "").strip()
        proto = (request.form.get("ftp_protocol") or "ftps").strip().lower()
        transport = normalize_access_protocol(proto)
        host = (request.form.get("ftp_host") or "").strip()
        default_port = 22 if transport == "sftp" else 21
        port = int(request.form.get("ftp_port") or default_port)
        user = (request.form.get("ftp_user") or "").strip()
        password = normalize_password(request.form.get("ftp_password") or "")
        password_env = (request.form.get("ftp_password_env") or "").strip()
        remote_root = (request.form.get("ftp_remote_root") or "/").strip() or "/"

        if not password and not password_env and account_id:
            try:
                existing = load_config().account(validate_account_id(account_id))
                password = existing.ftp.password
                password_env = password_env or existing.ftp.password_env
            except (KeyError, ValueError):
                pass

        secret = resolve_secret(password, password_env)
        if not host or not user:
            raise ValueError("Host and user are required.")
        if not secret:
            raise ValueError("Enter the access password (or env var) first.")

        settings = Settings(
            protocol=transport,
            host=host,
            port=port,
            user=user,
            password=secret,
            tls_explicit=request.form.get("ftp_tls") == "on",
            passive=request.form.get("ftp_passive") == "on",
            remote_files_root=remote_root.rstrip("/") or "/",
            local_backup_root=Path("."),
            local_files_dir=Path("."),
            local_mysql_dir=Path("."),
            incremental=True,
            verify_tls=request.form.get("ftp_verify_tls") == "on",
            host_key_policy=(request.form.get("ftp_host_key_policy") or "tofu").strip().lower(),
        )
        if proto == "ftps":
            settings.protocol = "ftps"
            settings.tls_explicit = True
        elif proto == "ftp":
            settings.protocol = "ftp"
            settings.tls_explicit = False
        settings.log_dir = logs_dir()
        return settings, account_id, host, port, user

    def _back_to_account_form(account_id: str, *, tab: str = "access"):
        if account_id:
            try:
                return redirect(
                    url_for("edit_account", account_id=validate_account_id(account_id)) + f"#{tab}"
                )
            except ValueError:
                pass
        return redirect(url_for("new_account") + f"#{tab}")

    @app.post("/accounts/test-ftp")
    def test_ftp_connection():
        """Test access using the Edit form fields (password typed now, before Save)."""
        from avidbackup.remote import connect

        account_id = (request.form.get("id") or "").strip()
        try:
            settings, account_id, host, port, user = _form_access_settings()
        except ValueError as exc:
            flash(str(exc), "bad")
            return _back_to_account_form(account_id, tab="access")

        try:
            with connect(settings) as client:
                entries = client.list_dir(settings.remote_files_root or "/")
            flash(
                f"Connection OK - logged in as {user}@{host}:{port} "
                f"({len(entries)} items in {settings.remote_files_root}). You can Save now.",
                "ok",
            )
        except Exception as exc:  # noqa: BLE001
            flash(f"Connection failed: {exc}", "bad")
        return _back_to_account_form(account_id, tab="access")

    @app.post("/accounts/discover-db")
    def discover_db_from_site():
        """Scan site config over FTP/SFTP for MySQL credentials (JSON for the form)."""
        from avidbackup.mysql_config import discover_all_mysql_credentials, resolve_direct_host
        from avidbackup.remote import connect

        wants_json = (
            request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in (request.headers.get("Accept") or "")
        )
        account_id = (request.form.get("id") or "").strip()
        try:
            settings, account_id, host, _port, _user = _form_access_settings()
        except ValueError as exc:
            if wants_json:
                return jsonify({"ok": False, "error": str(exc)}), 400
            flash(str(exc), "bad")
            return _back_to_account_form(account_id, tab="databases")

        try:
            with connect(settings) as client:
                # Prefer already-downloaded day files when present; else remote deep scan.
                local_files = None
                root = settings.local_backup_root
                try:
                    from avidbackup.daystore import iter_day_names

                    for day in reversed(iter_day_names(root)):
                        candidate = root / day / "files"
                        if candidate.is_dir():
                            local_files = candidate
                            break
                    if local_files is None and (root / "files").is_dir():
                        local_files = root / "files"
                except Exception:  # noqa: BLE001
                    local_files = None
                found = discover_all_mysql_credentials(
                    client,
                    settings.remote_files_root,
                    settings.mysql_config_candidates,
                    local_files_dir=local_files,
                )
            full_rows: list[dict] = []
            public_rows: list[dict] = []
            for creds in found:
                direct = resolve_direct_host(
                    creds,
                    mysql_host_override="",
                    ftp_host=host,
                )
                full = {
                    "host": direct,
                    "port": int(creds.port or 3306),
                    "user": creds.user,
                    "password": creds.password,
                    "database": creds.database,
                    "source_path": creds.source_path,
                    "label": creds.database or "MySQL",
                }
                full_rows.append(full)
                # Never put the password in the JSON response (browser / logs).
                public_rows.append(
                    {
                        "host": direct,
                        "port": int(creds.port or 3306),
                        "user": creds.user,
                        "password_set": bool(creds.password),
                        "database": creds.database,
                        "source_path": creds.source_path,
                        "label": creds.database or "MySQL",
                    }
                )
            _store_discovered_creds(account_id, full_rows)
            if wants_json:
                return jsonify({"ok": True, "found": public_rows, "count": len(public_rows)})
            if not public_rows:
                flash("No database credentials found in common site config files.", "bad")
            else:
                flash(
                    f"Found {len(public_rows)} database config(s). Open the Databases tab and use "
                    "Fetch again (or switch to Auto-fetch) to apply them. Passwords stay on this PC.",
                    "ok",
                )
        except Exception as exc:  # noqa: BLE001
            log.exception("DB discover failed")
            if wants_json:
                return jsonify({"ok": False, "error": str(exc)}), 500
            flash(f"Could not fetch DB details: {exc}", "bad")
        return _back_to_account_form(account_id, tab="databases")

    @app.post("/accounts/save")
    def save_account():
        cfg = load_config()
        try:
            raw_id = (request.form.get("id") or "").strip()
            label = (request.form.get("label") or "").strip()
            domain = (request.form.get("domain") or label or raw_id).strip()
            account_id = validate_account_id(raw_id or new_account_id(domain or label or "site", cfg.accounts))
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("new_account"))

        site_url = (request.form.get("site_url") or f"https://{domain}").strip()
        enabled = request.form.get("enabled") == "on"
        existing = None
        try:
            existing = cfg.account(account_id)
        except KeyError:
            pass

        from avidbackup.secure_cli import normalize_password

        proto = (request.form.get("ftp_protocol") or "ftps").strip().lower()
        if proto not in {"ftp", "ftps", "sftp", "ssh"}:
            proto = "ftps"
        default_port = 22 if proto in {"sftp", "ssh"} else 21
        ftp = FtpConfig(
            protocol=proto,
            host=(request.form.get("ftp_host") or "").strip(),
            port=int(request.form.get("ftp_port") or default_port),
            user=(request.form.get("ftp_user") or "").strip(),
            # Keep @ # & ~ etc. — never require quotes; unwrap accidental quotes only.
            password=normalize_password(request.form.get("ftp_password") or ""),
            password_env=(request.form.get("ftp_password_env") or "").strip(),
            tls_explicit=(request.form.get("ftp_tls") == "on") or (proto == "ftps"),
            passive=request.form.get("ftp_passive") == "on",
            remote_root=(request.form.get("ftp_remote_root") or "/").strip() or "/",
            verify_tls=(request.form.get("ftp_verify_tls") == "on") or (proto == "ftps"),
            host_key_policy=(request.form.get("ftp_host_key_policy") or "tofu").strip().lower(),
        )
        if proto in {"ftp", "ftps"} and "ftp_passive" not in request.form:
            ftp.passive = True
        if not ftp.password and existing and existing.ftp.password:
            ftp.password = existing.ftp.password
        if not ftp.password_env and existing:
            ftp.password_env = existing.ftp.password_env

        databases: list[DatabaseConfig] = []
        db_count = int(request.form.get("db_count") or 0)
        for i in range(db_count):
            db_type = (request.form.get(f"db_type_{i}") or "mysql").strip().lower()
            db_name = (request.form.get(f"db_database_{i}") or "").strip()
            db_host = (request.form.get(f"db_host_{i}") or "").strip()
            db_path = (request.form.get(f"db_path_{i}") or "").strip()
            if not db_name and not db_host and not db_path and request.form.get(f"db_discover_{i}") != "on":
                continue
            db_id = (request.form.get(f"db_id_{i}") or "").strip() or f"db{i+1}"
            password = request.form.get(f"db_password_{i}") or ""
            password_env = (request.form.get(f"db_password_env_{i}") or "").strip()
            db_user = (request.form.get(f"db_user_{i}") or "").strip()
            if existing and not password:
                for old in existing.databases:
                    if old.id == db_id and old.password:
                        password = old.password
                        break
            if not password:
                password = _pop_discovered_password(
                    account_id,
                    database=db_name,
                    user=db_user,
                    host=db_host,
                )
            if existing and not password_env:
                for old in existing.databases:
                    if old.id == db_id and old.password_env:
                        password_env = old.password_env
                        break
            default_port = 5432 if db_type == "postgres" else (0 if db_type == "sqlite" else 3306)
            databases.append(
                DatabaseConfig(
                    id=db_id,
                    label=(request.form.get(f"db_label_{i}") or db_name or db_id).strip(),
                    type=db_type,
                    host=db_host,
                    port=int(request.form.get(f"db_port_{i}") or default_port),
                    user=db_user,
                    password=password,
                    password_env=password_env,
                    database=db_name,
                    path=db_path,
                    format=(request.form.get(f"db_format_{i}") or "plain").strip(),
                    compress=(request.form.get(f"db_compress_{i}") or cfg.compress_dumps or "gzip").strip(),
                    enabled=request.form.get(f"db_enabled_{i}") == "on",
                    discover_from_site=request.form.get(f"db_discover_{i}") == "on",
                    ssl=request.form.get(f"db_ssl_{i}") == "on",
                )
            )

        retention = RetentionConfig(
            mysql_days=int(request.form.get("mysql_days") or cfg.mysql_retention_days),
            files_forever=request.form.get("files_forever") == "on",
            files_days=int(request.form.get("files_days") or 0),
        )
        account = AccountConfig(
            id=account_id,
            label=label or account_id,
            domain=domain or account_id,
            site_url=site_url,
            enabled=enabled,
            ftp=ftp,
            databases=databases,
            retention=retention,
        )
        if not ftp.host or not ftp.user:
            flash("Access host and username are required.", "bad")
            return render_template("account_form.html", account=account, title="Fix account")

        upsert_account(cfg, account)
        save_config(cfg)
        flash(f"Saved account {account_id}.", "ok")
        return redirect(url_for("index"))

    @app.post("/accounts/<account_id>/delete")
    def remove_account(account_id: str):
        try:
            validate_account_id(account_id)
        except ValueError as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))
        cfg = load_config()
        delete_account(cfg, account_id)
        save_config(cfg)
        flash(f"Removed {account_id} from config (local data folder kept).", "ok")
        return redirect(url_for("index"))

    @app.post("/accounts/<account_id>/run")
    def run_backup(account_id: str):
        try:
            validate_account_id(account_id)
            load_config().account(account_id)
        except (KeyError, ValueError) as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))

        try:
            pid = spawn_cli("run", "--account", account_id, label=account_id)
        except OSError as exc:
            log.exception("Could not start detached backup for %s", account_id)
            flash(f"Could not start backup: {exc}", "bad")
            return redirect(url_for("account_live", account_id=account_id))

        if not pid:
            flash(
                f"Backup did not start for {account_id} (no process). "
                "Check the spawn log under logs/ and try again.",
                "bad",
            )
        else:
            flash(
                f"Backup started for {account_id} (pid {pid}). "
                "It keeps running in the background — watch Live + log.",
                "ok",
            )
        nxt = (request.form.get("next") or "").strip()
        if nxt == "index":
            return redirect(url_for("index"))
        return redirect(url_for("account_live", account_id=account_id))

    @app.post("/accounts/<account_id>/stop")
    def stop_backup(account_id: str):
        from avidbackup.job_control import stop_account_backup

        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
            settings = settings_from_account(cfg, account)
        except (KeyError, ValueError) as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))

        ok, message = stop_account_backup(
            settings.local_backup_root,
            account_id=account_id,
            history_db=settings.history_db,
        )
        flash(message, "ok" if ok else "bad")
        nxt = (request.form.get("next") or "").strip()
        if nxt == "index":
            return redirect(url_for("index"))
        return redirect(url_for("account_live", account_id=account_id))

    def _redirect_after_folder(account_id: str):
        nxt = (request.form.get("next") or "").strip()
        if nxt == "live":
            return redirect(url_for("account_live", account_id=account_id))
        return redirect(url_for("index"))

    @app.post("/accounts/<account_id>/open-folder")
    def open_folder(account_id: str):
        try:
            validate_account_id(account_id)
            cfg = load_config()
            account = cfg.account(account_id)
        except (KeyError, ValueError) as exc:
            flash(str(exc), "bad")
            return redirect(url_for("index"))

        root = _cfg_data_root(cfg)
        account_dir = _path_under_root(resolve_data_dir(cfg, account), root)
        if account_dir is None:
            abort(400, description="Invalid backup path")

        day_dir = _path_under_root(account_dir / day_stamp(), root)
        target = day_dir if day_dir is not None and day_dir.is_dir() else account_dir
        if not target.is_dir():
            flash(f"Folder not found yet: {target}", "bad")
            return _redirect_after_folder(account_id)

        try:
            _open_in_file_manager(target)
        except OSError as exc:
            log.warning("Could not open folder %s: %s", target, exc)
            flash("Could not open the folder in the file manager.", "bad")
            return _redirect_after_folder(account_id)
        flash(f"Opened {target}", "ok")
        return _redirect_after_folder(account_id)

    @app.post("/run-all")
    def run_all():
        cfg = load_config()
        enabled = [a.id for a in cfg.enabled_accounts()]
        if not enabled:
            flash("No enabled accounts to back up.", "bad")
            return redirect(url_for("index"))
        try:
            started = spawn_account_runs(enabled)
        except OSError as exc:
            log.exception("Could not start per-account backups")
            flash(f"Could not start backups: {exc}", "bad")
            return redirect(url_for("index"))
        parts = [f"{aid} (pid {pid or '…'})" for aid, pid in started]
        flash(
            f"Started {len(started)} independent backup process(es): "
            + "; ".join(parts)
            + ". Each site has its own process, lock, and log.",
            "ok",
        )
        return redirect(url_for("index"))

    return app


def run_ui(*, host: str = "127.0.0.1", port: int = 8787) -> int:
    cfg = load_config()
    token = (cfg.ui_token or os.environ.get("AVIDBACKUP_UI_TOKEN") or "").strip()
    if not is_loopback_host(host):
        if not token:
            print(
                f"ERROR: Refusing to bind UI to non-loopback host {host!r} without "
                "ui_token / AVIDBACKUP_UI_TOKEN. Set a token in settings or env, or bind to 127.0.0.1.",
                file=sys.stderr,
            )
            return 2
        print(
            f"WARNING: Binding AvidBackup UI to {host}:{port} (non-loopback). "
            "UI token authentication is required. Prefer SSH tunnel + 127.0.0.1.",
            file=sys.stderr,
        )
    app = create_app()
    print(f"AvidBackup UI -> http://{host}:{port}")
    print(f"Home: {app_root()}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0
