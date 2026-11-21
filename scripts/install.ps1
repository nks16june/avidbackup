# AvidBackup installer — Windows (PowerShell)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "==> AvidBackup install from $Root"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Error "Python 3.10+ not found. Install from https://www.python.org/downloads/ (check 'Add to PATH')."
}

& $py.Source -m venv .venv
$pip = Join-Path $Root ".venv\Scripts\python.exe"
& $pip -m pip install --upgrade pip
& $pip -m pip install -e ".[dev]"

New-Item -ItemType Directory -Force -Path "config","data","logs" | Out-Null
if (-not (Test-Path "accounts.json")) {
    & $pip -m avidbackup init
}

& (Join-Path $Root "scripts\create_desktop_shortcut.ps1")
& (Join-Path $Root "scripts\install_windows_task.ps1")
& (Join-Path $Root "scripts\install_agent.ps1")

Write-Host ""
Write-Host "Install complete — AvidBackup agent is installed to keep running at logon."
Write-Host "  Desktop:   AvidBackup - Check backups  (opens UI; agent stays up)"
Write-Host "  Agent:     .\.venv\Scripts\avidbackup.exe agent status"
Write-Host "  Open UI:   .\.venv\Scripts\avidbackup.exe agent open"
Write-Host "  Run now:   .\.venv\Scripts\avidbackup.exe run"
Write-Host "  Docs:      docs\INSTALL.md"
