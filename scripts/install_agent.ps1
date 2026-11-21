# Install the always-on AvidBackup agent (Windows logon task).
# Browser is optional — closing a tab does not stop the agent or backups.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Cli = Join-Path $Root ".venv\Scripts\avidbackup.exe"
if (-not (Test-Path $Cli)) {
    throw "Missing $Cli — run scripts\install.ps1 first"
}
& $Cli agent install
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ""
Write-Host "Agent stays running after you close the browser."
Write-Host "Open UI anytime:  $Cli agent open"
Write-Host "Status:           $Cli agent status"
Write-Host "Uninstall agent:  $Cli agent uninstall"
