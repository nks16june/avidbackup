# How AvidBackup compares

**Last reviewed: October 2026**

Vendor features and pricing change. This page states only high-level, verifiable differences. Always check each vendor’s own site before buying or deploying.

AvidBackup is **not affiliated** with Softaculous, BackupSF, SQLBackupAndFTP, UpdraftPlus, Duplicator, BlogVault, WinSCP, FileZilla, or Cyberduck.

## At a glance

| Category | Examples | Typical model | vs AvidBackup |
|---|---|---|---|
| Desktop site-backup tools | BackupSF, SQLBackupAndFTP | Commercial desktop apps; BackupSF’s free tier is commonly limited (e.g. 1 site / 1 database — verify current terms) | AvidBackup is free MIT, multi-site, local-first |
| FTP clients + scripting | WinSCP, FileZilla, Cyberduck | Free file transfer; you script dumps, retries, retention | AvidBackup adds DB dumps, retention, retries, UI, and an initial-full gate |
| WordPress plugins | UpdraftPlus, Duplicator | Run **on the server**, WordPress only; many remote destinations are paid | AvidBackup runs on **your** machine for any FTP/SFTP site (not WP-only) |
| Hosted backup services | BlogVault and similar | Backups stored in the vendor’s cloud; subscription | AvidBackup keeps data on **your** disk; no cloud agent, no telemetry |
| AvidBackup | — | Free MIT, FTP/FTPS/SFTP + MySQL/Postgres/SQLite, local-first | Designed for agencies/admins who want offline ownership of archives |

## Detail

### Desktop site-backup tools

Commercial Windows-oriented products often bundle FTP + database backup with a GUI. Free tiers, when offered, are usually capped (site count, database count, or destinations). AvidBackup covers a similar workflow (pull files + dumps) under an MIT license with no artificial site caps in the software itself—your limits are disk space and host access.

### FTP clients + scripting

WinSCP, FileZilla, and Cyberduck are excellent for interactive file transfer. They do not, by themselves, schedule multi-site backups, dump databases, enforce a zero-failure initial full, or keep DB retention policies. You can script those; AvidBackup ships that pipeline ready-made.

### WordPress plugins

Plugins restore and migrate **inside** WordPress. That is the right tool when you need one-click WP clone/migrate on the server. AvidBackup does not replace that: it pulls a local archive of files and databases for any site reachable by FTP/FTPS/SFTP (WordPress, Laravel, static, etc.) without installing software on the host.

### Hosted services

Hosted products store backups in their infrastructure and often include malware scanning or staging. You pay for convenience and off-site copies. AvidBackup is local-first: if you want off-site copies, you add your own sync (NAS, rclone, etc.—optional and outside the core tool today).

## When NOT to use AvidBackup

- You need **server-side** one-click WordPress restore/migration (use a WP plugin or host tool).
- You need **managed off-site cloud storage** with vendor SLA (use a hosted backup service).
- Your host blocks **remote MySQL/Postgres** and you cannot allowlist your IP or use SSH—file sync still works; DB dumps may not until you enable remote DB access (see Limitations).
- You need enterprise multi-tenant SaaS UI with SSO—AvidBackup is a local utility.

## Limitations

- **Remote database access:** Many shared hosts only allow MySQL/Postgres from `localhost` on the web server. AvidBackup dumps from your PC over the network, so the host must permit your IP (or you use a tunnel). If remote DB is blocked, file backup still works; DB backup will fail until access is granted.
- **Not a migration suite:** Restore is guided (print/execute dump commands); it is not a full site-builder migrator.
- **Unsigned Windows installers:** SmartScreen may warn until code signing is configured (see [PACKAGING.md](PACKAGING.md)).

### Planned fallbacks (roadmap — not shipped in v0.1.0)

- Optional token-protected PHP dump helper for hosts that block remote MySQL
- SSH tunnel assist for DB dumps
- Optional offsite sync hooks (e.g. rclone/S3) after local backup succeeds

## Fairness note

Competitor products may offer features AvidBackup does not (cloud storage, WP-specific migrators, paid support). Choose the tool that matches your threat model and workflow—not every site needs a local-first archive.
