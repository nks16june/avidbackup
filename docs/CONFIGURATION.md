# Configuration (`accounts.json`)

Single supported config file (created by `avidbackup init` or the UI).

**Never commit this file** — it may contain passwords. Prefer `password_env` when possible.

```json
{
  "version": 3,
  "data_root": "./data",
  "mysql_retention_days": 15,
  "files_keep_forever": true,
  "large_table_mb": 50,
  "download_retries": 3,
  "compress_dumps": "gzip",
  "ui_host": "127.0.0.1",
  "ui_port": 8787,
  "ui_token": "",
  "notify": {
    "webhook_url": "",
    "webhook_secret": "",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_password_env": "",
    "smtp_from": "",
    "smtp_to": "",
    "smtp_tls": true
  },
  "accounts": [
    {
      "id": "example.com",
      "label": "Example",
      "domain": "example.com",
      "site_url": "https://example.com",
      "enabled": true,
      "ftp": {
        "protocol": "ftps",
        "host": "ftp.example.com",
        "port": 21,
        "user": "example",
        "password": "",
        "password_env": "EXAMPLE_FTP_PASS",
        "tls_explicit": true,
        "passive": true,
        "remote_root": "/public_html",
        "verify_tls": true,
        "host_key_policy": "tofu"
      },
      "databases": [
        {
          "id": "main",
          "type": "mysql",
          "label": "Main",
          "host": "203.0.113.10",
          "port": 3306,
          "user": "app",
          "password": "",
          "password_env": "EXAMPLE_DB_PASS",
          "database": "app",
          "compress": "gzip",
          "enabled": true,
          "ssl": false
        }
      ],
      "retention": {
        "mysql_days": 15,
        "files_forever": true,
        "files_days": 0
      }
    }
  ]
}
```

### Field notes

- `id` — `^[A-Za-z0-9._-]+$` only (path-safe)
- `ftp.host_key_policy` — `tofu` (default) | `strict` | `insecure`
- `ftp.verify_tls` — FTPS certificate verification (default true)
- `databases[].type` — `mysql` (default) | `postgres` | `sqlite`
- `databases[].ssl` — MySQL/Postgres TLS (`PGSSLMODE=require` / PyMySQL ssl); default false
- `databases[].path` — SQLite file (local path, or remote path fetched via FTP/SFTP)
- `databases[].discover_from_site` — scan brand-neutral CMS configs (WordPress `define()`, Joomla, `.env`, generic PHP profile arrays) under the remote web root
- On Windows, POSIX `chmod 600` is a no-op — restrict NTFS ACLs on `accounts.json`
