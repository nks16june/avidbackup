# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Security

- UI sign-in on by default: a random token is generated on first run (`avidbackup token`); the launcher opens a fragment sign-in link
- Host header allow-list (DNS-rebinding protection) and Origin check on all POSTs
- Safe `next` redirects, login lockout, `SameSite=Strict` uniquely named session cookie, `Referrer-Policy: same-origin`
- FTP restore to the server is now preview, then typed site ID, with a single-use expiring ticket and an audit log
- Fonts are bundled (SIL OFL); the UI no longer contacts Google Fonts
- Phone alerts are off by default; public ntfy.sh gets generic messages unless you opt in to details
- Removed legacy third-party notifier integrations; notifications are now generic (ntfy, webhook, email)
- Windows: the port check no longer treats a busy port as free (`SO_REUSEADDR`)

## [0.1.0] — 2026-10-31

### Added

- Local-first multi-site backup CLI + Flask UI (Windows / macOS / Linux)
- `avidbackup app` launcher (browser + optional system tray via `[desktop]` extra)
- Docker image + compose example (UI token required for published bind)
- Comparison doc and first-run Download / 5-minute walkthrough in README
- winget/Scoop manifest templates under `packaging/` (submit after first Release)
- FTP / FTPS / SFTP file sync with retries, incremental manifest, initial-full gate
- Database dumpers: MySQL/MariaDB, PostgreSQL, SQLite (gzip + `.sha256`)
- Security: MySQL defaults file, SFTP host-key TOFU, FTPS cert verify, CSRF, loopback UI bind, account-id validation
- `verify`, `restore-db` (dry-run or `--yes` execute with same-name `--force` guard), `doctor`
- Windows PyInstaller spec + Inno Setup installer (CI release job)
- Optional webhook / SMTP notifications
- pytest + ruff + GitHub Actions CI
- **Backups** dashboard (`/backups`): day-by-day runs and database dumps, a coverage grid that shows missed and failed days per site, 7/30/90-day and per-account filters
- Optional on-demand PostgreSQL client tools (installer checkbox, Settings button, `avidbackup tools install-postgres`)

### Notes

- `accounts.json` is the only config format (no `.env` account flow)
- Softaculous is mentioned only as a loose UX comparison — no affiliation
