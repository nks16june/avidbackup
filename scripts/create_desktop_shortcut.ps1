# Desktop shortcut for the local install (Python venv in this folder).
# Does not use dist\avidbackup.exe unless -Packaged is passed.
param(
    [switch]$Packaged
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop) { throw "Could not resolve the Desktop folder." }

$Cli = Join-Path $Root ".venv\Scripts\avidbackup.exe"
$Windowed = Join-Path $Root "dist\avidbackup.exe"

if ($Packaged -and (Test-Path $Windowed)) {
    $Target = $Windowed
    $Args = "agent open"
} elseif (Test-Path $Cli) {
    $Target = $Cli
    $Args = "agent open"
} else {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { throw "Local AvidBackup venv not found. Run scripts\install.ps1 first." }
    $Target = $py.Source
    $Args = "-m avidbackup agent open"
}

$LinkPath = Join-Path $Desktop "AvidBackup - Check backups.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Link = $Shell.CreateShortcut($LinkPath)
$Link.TargetPath = $Target
$Link.Arguments = $Args
$Link.WorkingDirectory = $Root
$Link.WindowStyle = 1
$Link.Description = "Open AvidBackup UI (agent keeps running in the background)"
$Link.Save()

Write-Host "Desktop shortcut: $LinkPath"
Write-Host "  Target: $Target $Args"
