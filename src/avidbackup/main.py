from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import avidbackup
from avidbackup.databases.registry import dump_all_databases
from avidbackup.doctor import run_doctor
from avidbackup.history import HistoryStore
from avidbackup.initial_full import has_completed_initial_full, mark_initial_full_done
from avidbackup.locks import acquire_run_lock
from avidbackup.logging_setup import setup_logging
from avidbackup.notify import send_success
from avidbackup.paths import ensure_layout
from avidbackup.remote import connect
from avidbackup.retention import prune_mysql_dumps
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

    sub.add_parser("list", help="List configured accounts")
    sub.add_parser("init", help="Create config/data/logs and empty accounts.json")

    p_app = sub.add_parser(
        "app",
        help="Launch UI, open browser, optional system tray (Open / Run all / Quit)",
    )
    p_app.add_argument("--host", default=None)
    p_app.add_argument("--port", type=int, default=None)
    p_app.add_argument("--no-browser", action="store_true")
    p_app.add_argument("--no-tray", action="store_true", help="Console mode even if desktop extra is installed")

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
    settings.local_files_dir.mkdir(parents=True, exist_ok=True)
    settings.local_mysql_dir.mkdir(parents=True, exist_ok=True)
    try:
        acquire_run_lock(settings.local_backup_root, account_id)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    log_file = setup_logging(settings.log_dir, settings.log_level)
    history = HistoryStore(settings.history_db)
    run_id = history.start_run()
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
            with connect(settings) as client:
                try:
                    dump_result = dump_all_databases(settings, remote_client=client)
                except Exception as exc:  # noqa: BLE001
                    has_explicit = any(getattr(d, "enabled", True) for d in (settings.databases or []))
                    if has_explicit:
                        raise
                    log.warning("Database dump skipped/failed: %s", exc)
            keep = int(getattr(settings, "mysql_retention_days", None) or mysql_keep_days)
            pruned = prune_mysql_dumps(settings.local_mysql_dir, keep)
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

    if (
        file_stats is not None
        and file_stats.failed == 0
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

    db_failed = dump_result.failed if dump_result else 0
    failed = (file_stats.failed if file_stats else 0) + db_failed
    if needs_initial and (
        file_stats is None or file_stats.failed > 0 or not has_completed_initial_full(settings)
    ):
        failed = max(failed, 1)
    status = "success" if not failed else "failed"

    if file_stats is not None:
        print(
            f"[{account_id}] Files: downloaded={file_stats.downloaded} "
            f"skipped={file_stats.skipped} failed={file_stats.failed} "
            f"retries={file_stats.retries} bytes={file_stats.bytes}"
        )
    if dump_result is not None:
        print(
            f"[{account_id}] DB: dumps={len(dump_result.paths)} "
            f"failed={dump_result.failed} bytes={dump_result.bytes_written}"
        )

    notified = False
    if status == "success" and not getattr(args, "no_notify", False):
        msg = (
            f"AvidBackup OK for {account_id} ({settings.site_url}). "
            f"Files↓ {file_stats.downloaded if file_stats else 0}, "
            f"DB dumps {len(dump_result.paths) if dump_result else 0}."
        )
        notified = send_success(cfg, msg)

    history.finish_run(
        run_id,
        status=status,
        files_downloaded=file_stats.downloaded if file_stats else 0,
        files_skipped=file_stats.skipped if file_stats else 0,
        files_failed=file_stats.failed if file_stats else 0,
        files_bytes=file_stats.bytes if file_stats else 0,
        mysql_downloaded=len(dump_result.paths) if dump_result else 0,
        mysql_bytes=dump_result.bytes_written if dump_result else 0,
        mysql_path=str(dump_result.paths[-1]) if dump_result and dump_result.paths else None,
        mysql_config_source=dump_result.source if dump_result else None,
        notify_sent=notified,
        error=None if status == "success" else f"{failed} failures",
    )
    return 0 if status == "success" else 1


def cmd_init() -> int:
    root = ensure_layout()
    cfg = load_config()
    save_config(cfg)
    print(f"AvidBackup ready at {root}")
    print(f"  accounts: {root / 'accounts.json'}")
    print(f"  data:     {cfg.data_root}")
    print("Next: avidbackup ui")
    return 0


def cmd_list() -> int:
    cfg = load_config()
    if not cfg.accounts:
        print("No accounts yet. Run: avidbackup ui")
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


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Accounts error: {exc}", file=sys.stderr)
        return 2
    selected = list(args.account or []) or [a.id for a in cfg.enabled_accounts()]
    if not selected:
        print("No accounts enabled. Add one with: avidbackup ui", file=sys.stderr)
        return 2
    worst = 0
    from avidbackup.validation import validate_account_id

    for account_id in selected:
        try:
            validate_account_id(account_id)
            account = cfg.account(account_id)
        except (KeyError, ValueError) as exc:
            print(exc, file=sys.stderr)
            worst = 2
            continue
        if not account.ftp.host:
            print(f"[{account_id}] missing FTP host — edit in UI", file=sys.stderr)
            worst = max(worst, 2)
            continue
        try:
            settings = settings_from_account(cfg, account)
        except Exception as exc:  # noqa: BLE001
            print(f"[{account_id}] config error: {exc}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        keep = int(account.retention.mysql_days or cfg.mysql_retention_days)
        code = _run_one(settings, args, account_id=account.id, mysql_keep_days=keep, cfg=cfg)
        worst = max(worst, code)
    return worst


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
            "Use: avidbackup {app,ui,run,list,init,verify,restore-db,doctor,tools} — "
            "try avidbackup --help"
        )
        return 0
    if cmd == "init":
        return cmd_init()
    if cmd == "list":
        return cmd_list()
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
    if cmd == "run":
        return cmd_run(args)
    return 2


def cli() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    cli()
