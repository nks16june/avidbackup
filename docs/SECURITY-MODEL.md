# Security model

## Assets

- FTP/SFTP credentials and DB passwords in `accounts.json`
- Website file archives and DB dumps under `data/`
- SFTP host keys in `config/known_hosts`
- Flask session key in `config/ui_secret_key`

## Threats we mitigate

| Threat | Mitigation |
|---|---|
| Password in `ps`/process list | MySQL option file / `PGPASSFILE` / env — never `--password=` on argv |
| CSRF on local UI | Per-session CSRF token on POSTs |
| Accidental public UI bind | Non-loopback refused without `ui_token` / `AVIDBACKUP_UI_TOKEN` |
| Path traversal via account id | Strict `^[A-Za-z0-9._-]+$` |
| SFTP MITM | Host-key TOFU / strict |
| FTPS MITM | `verify_tls` default true |
| Password echo in HTML | Blank inputs + “leave blank to keep” |

## Out of scope

- Full disk encryption (use OS/LUKS/BitLocker)
- Multi-user authenticated SaaS UI
- Remote attestation / cloud sync

## Reporting

See [SECURITY.md](../SECURITY.md).
