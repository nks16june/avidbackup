# Install

## Prerequisites

See [PREREQUISITES.md](PREREQUISITES.md).

## Windows installer

1. Download `AvidBackup-Setup-*.exe` from [GitHub Releases](https://github.com/avidwebsolutions/avidbackup/releases/latest).
2. Verify SHA256 against the `.sha256` file on the Release (see [PACKAGING.md](PACKAGING.md)).
3. Run the installer (per-user by default). SmartScreen may warn on unsigned builds — **More info → Run anyway** until SignPath signing is enabled.
4. Start **AvidBackup** from the Start Menu — UI opens in your browser (no terminal).

Optional: desktop icon and daily scheduled task during setup.

## pipx / pip (developer path)

```bash
pipx install avidbackup
avidbackup app                 # UI + browser; tray with: pipx inject avidbackup 'avidbackup[desktop]'
```

Or: `pip install avidbackup` / from clone `pip install -e ".[dev,desktop]"`.

## Docker (NAS / VPS)

```bash
export AVIDBACKUP_UI_TOKEN="$(openssl rand -hex 24)"   # required
docker compose up -d --build
# Open http://localhost:8787 and sign in with the token
docker compose exec -T avidbackup avidbackup run
```

Schedule backups with host cron calling `docker compose exec -T avidbackup avidbackup run`.  
Without `AVIDBACKUP_UI_TOKEN` the container **refuses to start**.

## From source

```bash
git clone https://github.com/avidwebsolutions/avidbackup.git
cd avidbackup
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,desktop]"
avidbackup init
avidbackup doctor
avidbackup app
```

Helpers: `scripts/install.sh` · `scripts/install.ps1`  
Home override: `export AVIDBACKUP_HOME=$HOME/avidbackup-home`

## Commands

```bash
avidbackup --version
avidbackup app | ui | init | list | run | verify | doctor | restore-db
avidbackup run --account example.com
avidbackup run --files-only | --db-only
```

`app` = UI + browser + optional tray. `ui` = Flask only (scripts/Docker).

## Schedule (native install)

- Windows: `scripts/install_windows_task.ps1` → `AvidBackup-Daily`  
  Uninstall: `Unregister-ScheduledTask -TaskName AvidBackup-Daily`
- macOS/Linux: `scripts/install_cron.sh`  
  Uninstall: remove the crontab line

## Uninstall

Remove the install dir / `AVIDBACKUP_HOME`, scheduled task/cron, and Docker volume if used (`docker compose down -v`).

## Package managers (after first Release)

winget/Scoop templates live under `packaging/` — submit once the installer SHA256 is known ([PACKAGING.md](PACKAGING.md)).  
macOS `.pkg`/`.dmg` and Linux `.deb`/AppImage are **planned**, not built yet.
