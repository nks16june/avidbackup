# Restore guide

**Always restore to a staging copy first.** Never overwrite production until verified.

## Website files

Local archive: `data/{account-id}/files/`

Re-upload with your FTP/SFTP client, or:

```bash
# example rsync over SSH (if you have SSH access)
rsync -av --dry-run data/example.com/files/ user@server:/path/to/public_html/
```

AvidBackup never deletes local files when they disappear on the server — your archive is the source of truth for recovery.

## Databases

Dry-run (default) prints the restore plan without executing:

```bash
avidbackup restore-db --account example.com --db main
```

Execute against a **staging** database (recommended):

```bash
avidbackup restore-db --account example.com --db main \
  --target-database app_staging \
  --yes
```

Restore into the **same** database name as the source (destructive) requires an explicit override:

```bash
avidbackup restore-db --account example.com --db main --yes --force
```

Options:

| Flag | Meaning |
|---|---|
| `--file PATH` | Dump file (default: newest matching dump) |
| `--target-host HOST` | Override host |
| `--target-database NAME` | Destination DB name |
| `--target-path PATH` | SQLite destination file |
| `--yes` | Execute (otherwise print-only) |
| `--force` | Allow target name/path equal to source |

Passwords are never placed on the process argv (`--defaults-extra-file` / `PGPASSFILE`).

### MySQL / MariaDB

Uses the `mysql` client with a temporary option file.

### PostgreSQL

Plain SQL via `psql` stdin, or custom format via `pg_restore`. SSL uses `PGSSLMODE=require` when the account DB entry has `ssl: true`.

### SQLite

```bash
avidbackup restore-db --account example.com --db localcache \
  --target-path ./restored.sqlite --yes
```

Overwriting the configured source path requires `--force`. Stop the app using the DB before replacing a live file.

## Verify after restore

```bash
avidbackup verify --account example.com
```
