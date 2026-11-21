# Packaging & distribution

**Last reviewed: October 2026**

## pip / pipx (developer path)

```bash
pipx install avidbackup
avidbackup app
# or:
pip install avidbackup
python -m avidbackup app
```

From a clone: `pip install -e ".[dev]"` · tray icons: `pip install -e ".[desktop]"`.

Console script `avidbackup` and package data (HTML templates) are declared in `pyproject.toml`.

## Windows installer [build]

1. `pip install -e ".[dev,desktop]" pyinstaller`
2. `pyinstaller --noconfirm build/pyinstaller/avidbackup.spec` → `dist/avidbackup.exe` (windowed; launches UI + browser; tray if bundled)
3. Inno Setup:  
   `"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.1.0 /DDistDir=%CD%\dist installer\avidbackup.iss`  
   → `dist/AvidBackup-Setup-<version>.exe`

CI (`.github/workflows/release.yml`) builds these on tag `v*` and attaches the installer plus a `.sha256` file.

### SmartScreen / code signing

Unsigned installers often show Windows SmartScreen: **More info → Run anyway**. That is expected until signing is configured.

**Planned fix:** [SignPath Foundation](https://signpath.org/) free code signing for open-source projects (workflow step is commented in `release.yml` — not claimed as already active).

**Always publish SHA256** next to the installer. Verify:

```powershell
Get-FileHash .\AvidBackup-Setup-0.1.0.exe -Algorithm SHA256
# compare to AvidBackup-Setup-0.1.0.exe.sha256 from the Release
```

```bash
sha256sum -c AvidBackup-Setup-0.1.0.exe.sha256
```

## Docker [build]

```bash
export AVIDBACKUP_UI_TOKEN='long-random-token'
docker compose up -d --build
# UI: http://localhost:8787  (token required)
docker compose exec -T avidbackup avidbackup run
```

The image refuses to start without `AVIDBACKUP_UI_TOKEN` (entrypoint + app bind rules). Data volume: `/data` (`AVIDBACKUP_HOME`). Includes `mariadb-client` and `postgresql-client`.

NAS/VPS: schedule `docker compose exec -T avidbackup avidbackup run` via the host cron/Task Scheduler.

## winget / Scoop [document only — submit after first release]

Templates (placeholder SHA256):

- `packaging/winget/AvidWebSolutions.AvidBackup.yaml`
- `packaging/scoop/avidbackup.json`

Point `InstallerUrl` / `url` at the GitHub Release asset, paste the real SHA256, then submit to [winget-pkgs](https://github.com/microsoft/winget-pkgs) / a Scoop bucket. Do not publish manifests before the Release exists.

## macOS / Linux packages [planned — not built]

| Format | Status |
|---|---|
| macOS `.pkg` / `.dmg` | Planned |
| Linux `.deb` / AppImage | Planned |

Until then use pipx, Docker, or source install on those platforms.

## Screenshots (`docs/images/`)

The README images are taken from a throwaway demo home folder with fake `example.com` sites
(no real hostnames, passwords or personal paths). To refresh them, seed a demo home, start
`avidbackup ui` against it, and capture `/`, `/backups`, a restore day, the restore confirmation
and `/accounts/new`. `social-preview.png` (1280x640) is the image GitHub shows when the repo
is shared. Keep real hostnames, passwords and local paths out of every screenshot.
