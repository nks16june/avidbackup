from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import avidbackup
from avidbackup.databases.registry import dump_all_databases
from avidbackup.daystore import dump_directories, prepare_day_dirs
from avidbackup.digest import build_digest
from avidbackup.doctor import run_doctor
from avidbackup.history import HistoryStore
from avidbackup.initial_full import has_completed_initial_full, mark_initial_full_done
from avidbackup.locks import acquire_run_lock
from avidbackup.logging_setup import setup_logging
from avidbackup.notify import channels_configured, ensure_phone_config, send_status
from avidbackup.paths import app_root, ensure_layout
from avidbackup.remote import connect
from avidbackup.retention import prune_mysql_dumps
from avidbackup.run_guard import register_active_run, unregister_active_run
from avidbackup.settings_builder import settings_from_account
from avidbackup.store import load_config, save_config
from avidbackup.sync_files import sync_changed_files
from avidbackup.verify import verify_all

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="avidbackup",
        description=(
            "AvidBackup - free, local-first backup utility for websites and databases. "
            "Pulls files (FTP/FTPS/SFTP) and database dumps to your own machine."
        ),
    )
    p.add_argument("--version", action="store_true", help="Print version and exit")
    sub = p.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run backup for one or all accounts")
    p_run.add_argument("--account", action="append", default=[])
    p_run.add_argument("--files-only", action="store_true")
    p_run.add_argument("--mysql-only", action="store_true", help="Databases only")
    p_run.add_argument("--db-only", action="store_true", help="Alias for --mysql-only")
    p_run.add_argument("--full", action="store_true")
    p_run.add_argument("--dry-run-list", action="store_true")
    p_run.add_argument("--max-files", type=int, default=None)
    p_run.add_argument("--no-notify", action="store_true")
    p_run.add_argument(
        "--serial",
        action="store_true",
        help="Run accounts one after another in this process (default: one OS process per site)",
    )

    p_digest = sub.add_parser("digest", help="Send today's backup status to your phone")
    p_digest.add_argument("--no-notify", action="store_true", help="Print the status without sending")

    sub.add_parser(
        "snapshot",
        help="Create today's files/ and db/ folders from what is already on disk",
    )

    sub.add_parser("list", help="List configured accounts")
    sub.add_parser("init", help="Create config/data/logs and empty accounts.json")

    p_app = sub.add_parser(
        "app",
        help="Run the local agent (API + tray). Browser is optional; use --no-browser for headless.",
    )
    p_app.add_argument("--host", default=None)
    p_app.add_argument("--port", type=int, default=None)
    p_app.add_argument("--no-browser", action="store_true")
    p_app.add_argument("--no-tray", action="store_true", help="Headless agent even if desktop extra is installed")

    p_agent = sub.add_parser(
        "agent",
        help="Install/start the always-on Windows agent (survives closing the browser)",
    )
    agent_sub = p_agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("install", help="Register logon autostart and start the agent now")
    agent_sub.add_parser("uninstall", help="Remove logon autostart (does not delete backups)")
    agent_sub.add_parser("status", help="Show whether the agent is installed and running")
    agent_sub.add_parser("start", help="Start the agent now")
    agent_sub.add_parser("open", help="Open the UI in a browser (starts agent if needed)")

    p_ui = sub.add_parser("ui", help="Start the local management UI (no tray; for scripts)")
    p_ui.add_argument("--host", default=None)
    p_ui.add_argument("--port", type=int, default=None)

    p_ver = sub.add_parser("verify", help="Verify local files + newest dumps")
    p_ver.add_argument("--account", action="append", default=[])

    p_restore = sub.add_parser(
        "restore-db",
        help="Dry-run (default) or execute a DB restore; see docs/RESTORE.md",
    )
    p_restore.add_argument("--account", required=True)
    p_restore.add_argument("--db", required=True, help="Database entry id")
    p_restore.add_argument("--file", type=Path, default=None)
    p_restore.add_argument("--target-host", default=None)
    p_restore.add_argument(
        "--target-database",
        default=None,
        help="Destination DB name (defaults to source; same-name requires --force)",
    )
    p_restore.add_argument(
        "--target-path",
        type=Path,
        default=None,
        help="SQLite destination file path",
    )
    p_restore.add_argument(
        "--yes",
        action="store_true",
        help="Execute restore (default prints the plan only)",
    )
    p_restore.add_argument(
        "--force",
        action="store_true",
        help="Allow restoring into the source database name / source SQLite path",
    )

    sub.add_parser("doctor", help="Check Python, tools, and write permissions")

    p_helper = sub.add_parser(
        "helper",
        help="PHP helper for MySQL on hosts that block remote MySQL and have no SSH",
    )
    helper_sub = p_helper.add_subparsers(dest="helper_command", required=True)
    for name, text in (
        ("install", "Upload the helper to the site and switch the database to it"),
        ("test", "Check that the helper answers and can reach the database"),
        ("remove", "Delete the helper from the site"),
    ):
        hp = helper_sub.add_parser(name, help=text)
        hp.add_argument("--account", required=True)
        hp.add_argument("--db", required=True, help="Database entry id")
        if name == "install":
            hp.add_argument(
                "--url",
                default="",
                help="Folder URL the upload directory is served at (default: the site URL)",
            )
            hp.add_argument(
                "--allowed-ip",
                default="",
                help="Only accept requests from these addresses (comma separated)",
            )

    p_dbtest = sub.add_parser(
        "db-test", help="Test how AvidBackup reaches a database (direct, SSH tunnel or PHP helper)"
    )
    p_dbtest.add_argument("--account", required=True)
    p_dbtest.add_argument("--db", default="", help="Database entry id (default: every database)")

    p_token = sub.add_parser("token", help="Print the UI sign-in token and sign-in link")
    p_token.add_argument("--rotate", action="store_true", help="Generate a new token (signs everyone out)")

    p_tools = sub.add_parser("tools", help="Optional helper tools (PostgreSQL client)")
    tools_sub = p_tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser(
        "install-postgres",
        help="Download pg_dump/psql/pg_restore (Windows, ~5 MB, checksum-verified)",
    )
    tools_sub.add_parser("status", help="Show which database tools were found")
    return p.parse_args(argv)


def _run_one(settings, args: argparse.Namespace, *, account_id: str, mysql_keep_days: int, cfg) -> int:
    if getattr(args, "full", False):
        settings.incremental = False
    if getattr(args, "max_files", None) is not None:
        settings.max_files = max(0, args.max_files)

    settings.local_backup_root.mkdir(parents=True, exist_ok=True)
    files_dir, db_dir = prepare_day_dirs(settings.local_backup_root)
    settings.local_files_dir = files_dir
    settings.local_mysql_dir = db_dir
    settings.local_files_dir.mkdir(parents=True, exist_ok=True)
    settings.local_mysql_dir.mkdir(parents=True, exist_ok=True)
    try:
        release_lock = acquire_run_lock(settings.local_backup_root, account_id)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    run_id: int | None = None
    try:
        log_file = setup_logging(settings.log_dir, settings.log_level, account_id=account_id)
        history = HistoryStore(settings.history_db)
        history.close_stale_runs(account_id=account_id)
        run_id = history.start_run(account_id=account_id)
        register_active_run(settings.history_db, run_id, account_id)
        log.info("Account %s — backup run #%s (log=%s)", account_id, run_id, log_file)

        db_only = getattr(args, "mysql_only", False) or getattr(args, "db_only", False)
        do_files = not db_only
        do_db = not getattr(args, "files_only", False)
        file_stats = None
        dump_result = None

        needs_initial = do_files and not has_completed_initial_full(settings)
        if needs_initial:
            if settings.incremental and not getattr(args, "full", False):
                log.info(
                    "Account %s — INITIAL FULL required; refusing incremental until a "
                    "zero-failure full completes (forcing full sync this run).",
                    account_id,
                )
            settings.incremental = False
            log.info("Account %s — INITIAL FULL required.", account_id)

        try:
            if getattr(args, "dry_run_list", False):
                with connect(settings) as client:
                    for e in client.list_dir(settings.remote_files_root):
                        kind = "DIR " if e.is_dir else "FILE"
                        print(f"  [{account_id}] {kind} {e.path}  size={e.size}")
                history.finish_run(run_id, status="dry-run")
                return 0

            if do_files:
                file_stats = sync_changed_files(settings, account_id=account_id)
            if do_db:
                from avidbackup.databases.base import DumpResult as _DumpResult

                with connect(settings) as client:
                    try:
                        dump_result = dump_all_databases(settings, remote_client=client)
                    except Exception as exc:  # noqa: BLE001
                        # Never swallow into a silent "success with no dump".
                        log.error("Database dump failed for %s: %s", account_id, exc)
                        dump_result = _DumpResult(failed=1, source=str(exc))
                keep = int(getattr(settings, "mysql_retention_days", None) or mysql_keep_days)
                pruned = 0
                for folder in dump_directories(settings.local_backup_root):
                    pruned += prune_mysql_dumps(folder, keep)
                if pruned:
                    log.info("Pruned %s dump(s) older than %s days", pruned, keep)
        except Exception as exc:  # noqa: BLE001
            log.exception("Backup failed for %s: %s", account_id, exc)
            history.finish_run(
                run_id,
                status="failed",
                files_downloaded=file_stats.downloaded if file_stats else 0,
                files_skipped=file_stats.skipped if file_stats else 0,
                files_failed=file_stats.failed if file_stats else 0,
                files_bytes=file_stats.bytes if file_stats else 0,
                mysql_downloaded=len(dump_result.paths) if dump_result else 0,
                mysql_bytes=dump_result.bytes_written if dump_result else 0,
                error=str(exc),
            )
            return 1

        file_failed = file_stats.failed if file_stats else 0
        warnings = file_stats.warnings if file_stats else 0
        dump_count = len(dump_result.paths) if dump_result else 0
        db_failed = int(dump_result.failed) if dump_result else (1 if do_db else 0)
        files_ok = (file_stats is None) or (file_failed == 0)
        # DB is expected whenever this run includes dumps (registry always attempts
        # at least discover). Zero dumps with do_db => not a full success.
        dumps_ok = (not do_db) or (dump_count > 0 and db_failed == 0)

        # Files baseline may complete even when DB is partial — but only mark
        # initial-full when files succeeded AND we are not leaving a required DB miss hidden.
        if (
            file_stats is not None
            and files_ok
            and dumps_ok
            and not (settings.max_files and settings.max_files > 0)
            and not has_completed_initial_full(settings)
        ):
            mark_initial_full_done(
                settings,
                account_id=account_id,
                downloaded=file_stats.downloaded,
                skipped=file_stats.skipped,
                bytes_copied=file_stats.bytes,
            )
            log.info("Account %s — INITIAL FULL COMPLETE.", account_id)
        elif (
            file_stats is not None
            and files_ok
            and not dumps_ok
            and not has_completed_initial_full(settings)
            and not (settings.max_files and settings.max_files > 0)
        ):
            # Allow incremental file resume after a clean file pass even if DB failed.
            # Product status stays partial until a dump succeeds.
            mark_initial_full_done(
                settings,
                account_id=account_id,
                downloaded=file_stats.downloaded,
                skipped=file_stats.skipped,
                bytes_copied=file_stats.bytes,
            )
            log.info(
                "Account %s — files baseline complete; database still incomplete (partial).",
                account_id,
            )

        if not files_ok or (
            needs_initial
            and file_stats is not None
            and file_failed > 0
        ):
            status = "failed"
        elif do_db and not dumps_ok:
            status = "partial"
        else:
            status = "success"

        if file_stats is not None:
            print(
                f"[{account_id}] Files: downloaded={file_stats.downloaded} "
                f"skipped={file_stats.skipped} failed={file_stats.failed} "
                f"warnings={file_stats.warnings} "
                f"retries={file_stats.retries} bytes={file_stats.bytes}"
            )
        if dump_result is not None:
            print(
                f"[{account_id}] DB: dumps={dump_count} "
                f"failed={db_failed} bytes={dump_result.bytes_written}"
            )

        if warnings:
            log.warning(
                "Account %s finished with %s file(s) the server refused to read "
                "(left out — listed in Live → Done/not done).",
                account_id,
                warnings,
            )
        db_note = ""
        if do_db and not dumps_ok:
            db_note = (dump_result.source if dump_result else "") or "database dump produced 0 files"
            log.error(
                "Account %s: PARTIAL — files OK but no database dump (%s). "
                "Allowlist this PC for Remote MySQL, or set DB credentials under Edit.",
                account_id,
                db_note,
            )

        err_msg = None
        if status == "failed":
            parts = []
            if file_failed:
                parts.append(f"{file_failed} file download failure(s)")
            if do_db and not dumps_ok:
                parts.append(f"database dump failed ({db_note})")
            err_msg = "; ".join(parts) or "backup failed"
        elif status == "partial":
            err_msg = f"Files OK; database dump failed ({db_note})"

        history.finish_run(
            run_id,
            status=status,
            files_downloaded=file_stats.downloaded if file_stats else 0,
            files_skipped=file_stats.skipped if file_stats else 0,
            files_failed=file_failed,
            files_bytes=file_stats.bytes if file_stats else 0,
            mysql_downloaded=dump_count,
            mysql_bytes=dump_result.bytes_written if dump_result else 0,
            mysql_path=str(dump_result.paths[-1]) if dump_result and dump_result.paths else None,
            mysql_config_source=dump_result.source if dump_result else None,
            notify_sent=False,
            error=err_msg,
            detail={
                "warnings": warnings,
                "left_out": list(getattr(file_stats, "left_out_paths", None) or [])[:50],
                "failed_paths": list(getattr(file_stats, "failed_paths", None) or [])[:50],
                "db_partial": status == "partial",
                "db_note": db_note,
                "files_ok": files_ok,
                "dumps_ok": dumps_ok,
                "files_dir": str(settings.local_files_dir),
                "db_dir": str(settings.local_mysql_dir),
                "resume": "Start/Resume skips unchanged files (size+mtime); only missing/changed download.",
            },
        )
        # success=0, partial=2, failed=1 — partial is not a clean success for monitors.
        if status == "success":
            return 0
        if status == "partial":
            return 2
        return 1
    finally:
        if run_id is not None:
            unregister_active_run(settings.history_db, run_id)
        release_lock()


def cmd_init() -> int:
    root = ensure_layout()
    cfg = load_config()
    save_config(cfg)
    print(f"AvidBackup ready at {root}")
    print(f"  accounts: {root / 'accounts.json'}")
    print(f"  data:     {cfg.data_root}")
    print("Next: avidbackup app")
    return 0


def cmd_list() -> int:
    cfg = load_config()
    if not cfg.accounts:
        print("No accounts yet. Run: avidbackup app")
        return 0
    for acc in cfg.accounts:
        flag = "on" if acc.enabled else "off"
        try:
            s = settings_from_account(cfg, acc)
            full = "initial-full-done" if has_completed_initial_full(s) else "NEEDS-INITIAL-FULL"
            data = s.local_backup_root
        except Exception:  # noqa: BLE001
            full = "config-error"
            data = "?"
        dbs = len([d for d in acc.databases if d.enabled])
        print(f"{acc.id}\t{flag}\t{full}\tdbs={dbs}\t{acc.label}\t{data}")
    return 0


def cmd_restore_db(args: argparse.Namespace) -> int:
    from avidbackup.restore_db import (
        build_restore_plan,
        describe_plan,
        execute_restore,
        newest_dump,
    )
    from avidbackup.validation import validate_account_id

    try:
        validate_account_id(args.account)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    cfg = load_config()
    try:
        account = cfg.account(args.account)
    except (KeyError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    db = next((d for d in account.databases if d.id == args.db), None)
    if db is None:
        print(f"Unknown db id {args.db!r}", file=sys.stderr)
        return 2
    settings = settings_from_account(cfg, account)
    path = args.file
    if path is None:
        path = newest_dump(settings.local_mysql_dir, db.database or Path(db.path or "db").stem)
        if path is None:
            print("No dump files found", file=sys.stderr)
            return 2
    if not Path(path).is_file():
        print(f"Dump not found: {path}", file=sys.stderr)
        return 2

    plan = build_restore_plan(
        db,
        Path(path),
        target_host=args.target_host,
        target_database=args.target_database,
        target_path=args.target_path,
    )
    print("Restore plan (see docs/RESTORE.md):")
    print(describe_plan(plan))
    print(
        f"  source_db={plan['source_database']!r} target_db={plan['target_database']!r} "
        f"force={bool(args.force)}"
    )
    print("Safety: restore to a staging database first.")
    if not args.yes:
        print("Re-run with --yes to execute. Same-name target also needs --force.")
        return 0
    return execute_restore(db, plan, force=bool(args.force))


def _run_account_job(
    cfg,
    args: argparse.Namespace,
    account_id: str,
) -> tuple[str, int, str]:
    """Run one account. Never raises — failures become an exit code + reason."""
    from avidbackup.validation import validate_account_id

    try:
        validate_account_id(account_id)
        account = cfg.account(account_id)
    except (KeyError, ValueError) as exc:
        return account_id, 2, str(exc)
    if not account.ftp.host:
        return account_id, 2, "missing FTP host — edit in UI"
    try:
        settings = settings_from_account(cfg, account)
    except Exception as exc:  # noqa: BLE001
        return account_id, 2, f"config error: {exc}"
    keep = int(account.retention.mysql_days or cfg.mysql_retention_days)
    try:
        code = _run_one(settings, args, account_id=account.id, mysql_keep_days=keep, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        log.exception("Backup crashed for %s", account.id)
        return account.id, 1, f"crashed: {exc}"
    if code == 0:
        return account.id, 0, "ok"
    if code == 3:
        return account.id, 3, "skipped — already running"
    return account.id, code, "failed — see account log and dashboard error"


def _forwarded_run_flags(args: argparse.Namespace) -> list[str]:
    """CLI flags to pass into per-account child processes."""
    out: list[str] = []
    if getattr(args, "files_only", False):
        out.append("--files-only")
    if getattr(args, "mysql_only", False) or getattr(args, "db_only", False):
        out.append("--db-only")
    if getattr(args, "full", False):
        out.append("--full")
    if getattr(args, "dry_run_list", False):
        out.append("--dry-run-list")
    if getattr(args, "max_files", None) is not None:
        out.extend(["--max-files", str(int(args.max_files))])
    return out


def _run_accounts_as_processes(selected: list[str], args: argparse.Namespace) -> list[tuple[str, int, str]]:
    """One OS process per account so sites never share memory or crash fate."""
    import subprocess

    from avidbackup.procutil import cli_command

    extras = _forwarded_run_flags(args)
    children: list[tuple[str, subprocess.Popen[str]]] = []
    print(
        f"Starting {len(selected)} independent backup process(es) "
        "(one process per site)…",
        flush=True,
    )
    for account_id in selected:
        cmd = cli_command("run", "--account", account_id, "--no-notify", *extras)
        print(f"  → {account_id}: {' '.join(cmd)}", flush=True)
        popen_kwargs: dict = {"cwd": str(app_root())}
        if sys.platform == "win32":
            # Keep multi-account CLI children off the desktop (no extra consoles).
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603
        children.append((account_id, proc))

    results: list[tuple[str, int, str]] = []
    for account_id, proc in children:
        code = int(proc.wait())
        if code == 0:
            reason = "ok"
        elif code == 3:
            reason = "skipped — already running"
        else:
            reason = "failed — see account log and dashboard error"
        results.append((account_id, code, reason))
    return results


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Accounts error: {exc}", file=sys.stderr)
        return 2
    selected = list(args.account or []) or [a.id for a in cfg.enabled_accounts()]
    if not selected:
        print("No accounts enabled. Add one with: avidbackup app", file=sys.stderr)
        return 2

    serial = bool(getattr(args, "serial", False)) or len(selected) == 1
    results: list[tuple[str, int, str]] = []
    if serial:
        print(f"Running {len(selected)} account(s) one after another.", flush=True)
        for account_id in selected:
            results.append(_run_account_job(cfg, args, account_id))
    else:
        # Separate OS process per site (not threads in one process).
        results = _run_accounts_as_processes(selected, args)

    worst = 0
    for account_id, code, reason in sorted(results, key=lambda row: row[0]):
        worst = max(worst, code)
        print(f"[{account_id}] exit={code} — {reason}", flush=True)

    if not getattr(args, "no_notify", False) and not getattr(args, "dry_run_list", False):
        _send_digest(cfg, selected)
    return worst


def _send_digest(cfg, account_ids: list[str] | None = None) -> bool:
    try:
        ensure_phone_config(cfg)
        title, message, urgent = build_digest(cfg, account_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not build the phone status: {exc}", file=sys.stderr)
        return False
    print(message)
    if not channels_configured(cfg):
        print("No notification channel is set up, so no status message was sent.")
        return True
    try:
        sent = send_status(cfg, title, message, urgent=urgent)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not send the phone status: {exc}", file=sys.stderr)
        return False
    print("Notification sent." if sent else "Notification was not delivered.")
    return sent


def cmd_digest(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Accounts error: {exc}", file=sys.stderr)
        return 2
    for account in cfg.accounts:
        try:
            settings = settings_from_account(cfg, account)
            HistoryStore(settings.history_db).close_stale_runs()
        except Exception:  # noqa: BLE001
            continue
    if getattr(args, "no_notify", False):
        _title, message, urgent = build_digest(cfg)
        print(message)
        return 1 if urgent else 0
    ensure_phone_config(cfg)
    title, message, urgent = build_digest(cfg)
    print(message)
    if not channels_configured(cfg):
        print("No notification channel is set up, so no status message was sent.")
        return 1 if urgent else 0
    sent = False
    try:
        sent = send_status(cfg, title, message, urgent=urgent)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not send the phone status: {exc}", file=sys.stderr)
    print("Notification sent." if sent else "Notification was not delivered.")
    return 0 if sent else 1


def cmd_snapshot() -> int:
    from avidbackup.store import resolve_data_dir

    cfg = load_config()
    if not cfg.accounts:
        print("No accounts yet.")
        return 2
    for account in cfg.accounts:
        root = resolve_data_dir(cfg, account)
        print(f"[{account.label}] linking today's folders…", flush=True)
        files_dir, db_dir = prepare_day_dirs(root)
        print(f"[{account.label}]")
        print(f"  files  {files_dir}")
        print(f"  db     {db_dir}")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from avidbackup.agent_service import (
        get_agent_status,
        install_agent,
        open_ui_or_start_agent,
        start_agent,
        uninstall_agent,
    )

    action = args.agent_command
    try:
        if action == "install":
            st = install_agent(start_now=True)
        elif action == "uninstall":
            st = uninstall_agent()
        elif action == "start":
            st = start_agent()
        elif action == "open":
            return open_ui_or_start_agent(open_browser=True)
        elif action == "status":
            st = get_agent_status()
        else:
            print(f"Unknown agent command: {action}", file=sys.stderr)
            return 2
    except OSError as exc:
        print(f"Agent error: {exc}", file=sys.stderr)
        return 1

    print(f"task:     {st.task_name}")
    print(f"installed:{st.installed}")
    print(f"running:  {st.running} (pid={st.pid})")
    print(f"ui:       {st.ui_url}")
    if st.detail:
        print(f"note:     {st.detail}")
    return 0 if (action in {"uninstall", "status"} or st.running or st.pid) else 1


def _find_database(cfg, account_id: str, db_id: str):  # noqa: ANN001, ANN202
    account = cfg.account(account_id)  # KeyError when unknown
    for db in account.databases:
        if db.id == db_id:
            return account, db
    known = ", ".join(d.id for d in account.databases) or "none"
    raise KeyError(f"No database {db_id!r} on {account_id} (databases: {known})")


def cmd_helper(args: argparse.Namespace) -> int:
    from avidbackup import db_connection
    from avidbackup.php_helper import HelperError

    ensure_layout()
    try:
        cfg = load_config()
        account, db = _find_database(cfg, args.account, args.db)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.helper_command == "install":
            info = db_connection.install_helper_for(
                cfg, account, db, base_url=args.url, allowed_ips=args.allowed_ip
            )
            save_config(cfg)
            print(f"Helper installed and answering at {info.url}")
            print(f"Uploaded to {info.path}. The secret token is saved in accounts.json (not printed).")
            print("Back up this database with: avidbackup run --account " + args.account + " --db-only")
            return 0
        if args.helper_command == "test":
            if db.via != "php":
                print("This database does not use the PHP helper (set up with: avidbackup helper install).")
                return 1
            print(db_connection.test_database_connection(cfg, account, db))
            return 0
        db_connection.remove_helper_for(cfg, account, db)
        save_config(cfg)
        print("Helper removed from the server. This database now connects directly again.")
        return 0
    except (HelperError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_db_test(args: argparse.Namespace) -> int:
    from avidbackup import db_connection

    ensure_layout()
    try:
        cfg = load_config()
        account = cfg.account(args.account)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    databases = [d for d in account.databases if not args.db or d.id == args.db]
    if not databases:
        print("No matching database.", file=sys.stderr)
        return 2
    worst = 0
    for db in databases:
        try:
            print(f"[{db.id}] {db_connection.test_database_connection(cfg, account, db)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{db.id}] FAILED: {exc}", file=sys.stderr)
            worst = 1
    return worst


def cmd_token(args: argparse.Namespace) -> int:
    from avidbackup.webui.security import (
        auth_disabled_by_env,
        load_or_create_ui_token,
        resolve_ui_token,
        signin_url,
    )

    ensure_layout()
    cfg = load_config()
    if args.rotate:
        if (cfg.ui_token or "").strip():
            print("A token is set in settings (ui_token); change it there.")
            return 1
        load_or_create_ui_token(rotate=True)
    token = resolve_ui_token(cfg.ui_token)
    if not token:
        print("Sign-in is switched off (AVIDBACKUP_UI_AUTH=off).")
        return 0
    base = f"http://127.0.0.1:{cfg.ui_port or 8787}"
    print(f"Token: {token}")
    print(f"Sign-in link: {signin_url(base, token)}")
    if auth_disabled_by_env():
        print("Note: AVIDBACKUP_UI_AUTH=off is ignored when a token is configured.")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from avidbackup.paths import logs_dir
    from avidbackup.pgtools import PgToolsError, install_postgres_tools
    from avidbackup.util_bin import find_pg_dump, find_pg_restore, find_psql

    if args.tools_command == "status":
        for label, found in (
            ("pg_dump", find_pg_dump()),
            ("pg_restore", find_pg_restore()),
            ("psql", find_psql()),
        ):
            print(f"{label}: {found or 'not found'}")
        return 0

    ensure_layout()
    log_file = logs_dir() / "tools.log"

    def say(msg: str) -> None:
        print(msg)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{msg}\n")

    try:
        install_postgres_tools(say)
    except PgToolsError as exc:
        say(f"ERROR: {exc}")
        return 1
    return 0


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"avidbackup {avidbackup.__version__}")
        return 0
    cmd = args.command
    if cmd is None:
        # Avoid SystemExit from argparse --help so callers/tests can handle the return code
        print(
            "Use: avidbackup {app,agent,ui,run,digest,snapshot,list,init,verify,restore-db,doctor,tools,token,helper,db-test} — "
            "try avidbackup --help"
        )
        return 0
    if cmd == "init":
        return cmd_init()
    if cmd == "list":
        return cmd_list()
    if cmd == "agent":
        return cmd_agent(args)
    if cmd == "app":
        from avidbackup.desktop_app import run_app

        return run_app(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            no_tray=bool(args.no_tray),
        )
    if cmd == "ui":
        from avidbackup.webui.app import run_ui

        cfg = load_config()
        return run_ui(host=args.host or cfg.ui_host, port=args.port or cfg.ui_port)
    if cmd == "verify":
        return verify_all(args.account or None)
    if cmd == "restore-db":
        return cmd_restore_db(args)
    if cmd == "doctor":
        return run_doctor()
    if cmd == "tools":
        return cmd_tools(args)
    if cmd == "token":
        return cmd_token(args)
    if cmd == "helper":
        return cmd_helper(args)
    if cmd == "db-test":
        return cmd_db_test(args)
    if cmd == "run":
        return cmd_run(args)
    if cmd == "digest":
        return cmd_digest(args)
    if cmd == "snapshot":
        return cmd_snapshot()
    return 2


def cli() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    cli()
