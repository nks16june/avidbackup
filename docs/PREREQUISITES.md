# Prerequisites

| Required | Notes |
|---|---|
| Python 3.10+ | 3.11/3.12 recommended |
| pip / venv | Bundled with most Python installs |

| Optional | Notes |
|---|---|
| mysqldump | Faster MySQL dumps (else PyMySQL) |
| pg_dump / psql / pg_restore | Only for PostgreSQL. **Windows:** tick "Download PostgreSQL backup tools" in the installer, or click the button under Settings > Optional tools, or run `avidbackup tools install-postgres` (about 5 MB, checksum-verified). **Linux/macOS:** `apt install postgresql-client` / `brew install libpq`. |
| cron / Task Scheduler | Unattended runs |

Install helpers detect missing tools via `avidbackup doctor`.
