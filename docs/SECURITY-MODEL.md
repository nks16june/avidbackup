# Security model

## Assets

- FTP/SFTP credentials and DB passwords in `accounts.json`
- Website file archives and DB dumps under `data/`
- SFTP host keys in `config/known_hosts`
- Flask session key in `config/ui_secret_key` and the sign-in token in `config/ui_token`
- The audit trail in `logs/audit.log` (restore previews/uploads, failed sign-ins)

## Threats we mitigate

| Threat | Mitigation |
|---|---|
| Password in `ps`/process list | MySQL option file / `PGPASSFILE` / env — never `--password=` on argv |
| CSRF on local UI | Per-session CSRF token on POSTs, plus an Origin check (cross-origin and `null` are refused) |
| DNS rebinding (a web page reaching the local UI through a hostile domain) | Host header must be `localhost`, `127.0.0.1` or `[::1]`; other hosts get HTTP 400. LAN names go in `AVIDBACKUP_ALLOWED_HOSTS` |
| Other programs/users on the same PC using the UI | Sign-in token generated on first run (`avidbackup token` prints it); sessions expire after 7 days; rotating the token signs everyone out; 5 failed sign-ins lock the client out for a minute |
| Token leaking into URLs/logs | Sign-in links carry the token in the URL fragment (`/login#t=...`), which browsers never send to the server |
| Open redirect after sign-in | `next` accepts local paths only |
| Cookie theft / cross-site use | Session cookie is `HttpOnly`, `SameSite=Strict`, uniquely named |
| Accidental or forged restore to the live server | Two steps: a preview of exactly what would be uploaded, then the exact site ID typed in; the preview ticket is single-use, expires after 10 minutes and is bound to the previewed folder; every step is written to `logs/audit.log` |
| Calling home | Fonts are bundled; the UI loads nothing from the internet. Phone alerts are off by default and, on the public ntfy.sh server, carry no site names unless you opt in |
| Accidental public UI bind | Non-loopback refused without `ui_token` / `AVIDBACKUP_UI_TOKEN` |
| Path traversal via account id | Strict `^[A-Za-z0-9._-]+$` |
| SFTP MITM | Host-key TOFU / strict |
| FTPS MITM | `verify_tls` default true |
| Password echo in HTML | Blank inputs + “leave blank to keep” |

## Sign-in and the environment switches

| Setting | Effect |
|---|---|
| `avidbackup token` | Prints the token and a sign-in link. `--rotate` makes a new one |
| `ui_token` in settings / `AVIDBACKUP_UI_TOKEN` | Use your own token instead of the generated one (required for non-loopback binds) |
| `AVIDBACKUP_UI_AUTH=off` | Disable sign-in on a single-user machine. Loopback only. Not recommended |
| `AVIDBACKUP_ALLOWED_HOSTS=nas.lan,backup.lan` | Extra Host names accepted by the Host check |
| `AVIDBACKUP_SPAWN_MODE=auto\|direct\|task` | Windows only. How a backup is started: a detached process (default, with Task Scheduler as fallback), detached only, or always via Task Scheduler |

The app opens your browser with a sign-in link automatically; you only need the token if you open the
address by hand or from another browser.

## Known limits

- `accounts.json` stores passwords in plain text. On Linux/macOS it is created `0600`; on Windows file
  permissions are not enforced the same way, so keep the folder in your user profile or encrypt the disk.
- Dumps and file backups are stored unencrypted.
- Pages still use inline scripts and styles (the CSP allows `'unsafe-inline'`), so output escaping is the
  main XSS defence.
- Anything running as your own user on the same machine can read the token file and `accounts.json`.

## Out of scope

- Full disk encryption (use OS/LUKS/BitLocker)
- Multi-user authenticated SaaS UI
- Remote attestation / cloud sync

## Reporting

See [SECURITY.md](../SECURITY.md).
