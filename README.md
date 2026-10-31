# AvidBackup

**Free, local-first backup utility for websites and databases.**  
Pulls files (FTP/FTPS/SFTP) and database dumps to **your own machine**. No cloud, no telemetry.

[![CI](https://github.com/avidwebsolutions/avidbackup/actions/workflows/ci.yml/badge.svg)](https://github.com/avidwebsolutions/avidbackup/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)

Built by **[Avid Web Solutions](https://avidwebsolutions.in)** · **Nishant Shrivastava** · Related: **[Avid ERP](https://aviderp.com)**

> Softaculous-*style* multi-site UI — **not affiliated** with Softaculous.

## Download

| Path | Command / link |
|---|---|
| **Windows installer** | [Latest GitHub Release](https://github.com/avidwebsolutions/avidbackup/releases/latest) → `AvidBackup-Setup-*.exe` (verify `.sha256`) |
| **pipx (any OS)** | `pipx install avidbackup` then `avidbackup app` |
| **Docker** | `docker compose up -d` (requires `AVIDBACKUP_UI_TOKEN`) — see [docs/INSTALL.md](docs/INSTALL.md) |

Developer/from source: `pip install -e ".[dev]"` · optional tray: `pip install "avidbackup[desktop]"`.

## First backup in 5 minutes

1. **Install** — Windows: run the Release installer (Start Menu → AvidBackup). Or: `pipx install avidbackup` / `avidbackup app`.
2. **Add a site** — In the UI, click Add website: FTP/SFTP host, optional databases, Save.
3. **Backup** — Click **Backup** on that account (or Run all). Wait for the initial full to finish with zero failures; later runs are incremental.

Screenshots (add under `docs/images/` — see checklist in [PACKAGING.md](docs/PACKAGING.md)):

| Placeholder | Shows |
|---|---|
| `docs/images/ui-dashboard.png` | Accounts list |
| `docs/images/ui-add-site.png` | Add/edit site form |
| `docs/images/ui-progress.png` | Transfer / run progress |

## How it compares

| | |
|---|---|
| Desktop tools (BackupSF, SQLBackupAndFTP) | Often commercial; free tiers commonly capped |
| FTP clients (WinSCP, FileZilla, Cyberduck) | Free files; you script DB dumps & retention |
| WordPress plugins (UpdraftPlus, Duplicator) | On-server, WP-only; many remotes paid |
| Hosted (BlogVault etc.) | Cloud-held backups, subscription |
| **AvidBackup** | Free MIT, any FTP/SFTP site + DB, local-first, no telemetry |

Full write-up (fairness notes, when *not* to use, limitations): **[docs/COMPARISON.md](docs/COMPARISON.md)**.

## Why

| | |
|---|---|
| **Local-first** | Archives on your disk (PC, NAS, VPS) |
| **Never deletes locals** | Remote deletes do not wipe your archive |
| **Initial full gate** | Incremental only after a zero-failure full |
| **Multi-DB** | MySQL/MariaDB, SQLite, PostgreSQL (client tools are an optional ~5 MB download on Windows) |
| **Secure defaults** | FTPS verify, SFTP TOFU, CSRF, loopback UI |

## Security & privacy

- Credentials in gitignored `accounts.json` (chmod 600 when the OS allows)
- Prefer `password_env`; UI non-loopback needs `AVIDBACKUP_UI_TOKEN`
- See [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md)

**Limitation:** many shared hosts block remote MySQL — allowlist your IP or wait for planned PHP/SSH dump helpers ([COMPARISON.md](docs/COMPARISON.md#limitations)).

## CLI (secondary)

```bash
avidbackup app          # UI + browser (+ tray if [desktop] installed)
avidbackup run          # backup enabled accounts
avidbackup doctor
avidbackup verify
avidbackup restore-db --account example.com --db main
```

## Docs

[INSTALL](docs/INSTALL.md) · [CONFIGURATION](docs/CONFIGURATION.md) · [RESTORE](docs/RESTORE.md) · [COMPARISON](docs/COMPARISON.md) · [PACKAGING](docs/PACKAGING.md) · [CHANGELOG](CHANGELOG.md)

## Roadmap

- [x] MySQL / MariaDB, PostgreSQL, SQLite  
- [ ] Optional token-protected PHP dump helper (hosts without remote MySQL)  
- [ ] SSH tunnel assist for DB dumps  
- [ ] MongoDB  
- [ ] Optional offsite sync (rclone/S3) after local success  
- [ ] macOS .pkg/.dmg and Linux .deb/AppImage (documented, not built yet)  
- [ ] Code signing (SignPath) for Windows SmartScreen  

## License

MIT © Avid Web Solutions / Nishant Shrivastava
