# AvidBackup runner (Task Scheduler–friendly)
# Examples:
#   .\scripts\run_backup.ps1
#   .\scripts\run_backup.ps1 run --account example.com
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BackupArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path (Join-Path $LogDir "scheduler.log") -Value "$Stamp START args=$($BackupArgs -join ' ')"

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

# Default to "run" when no subcommand is passed
if ($BackupArgs.Count -eq 0) {
    $BackupArgs = @("run")
} elseif ($BackupArgs[0] -notmatch '^(run|list|init|ui|verify|doctor|restore-db)$' -and $BackupArgs[0] -notlike '-*') {
    $BackupArgs = @("run") + $BackupArgs
} elseif ($BackupArgs[0] -like '--*') {
    $BackupArgs = @("run") + $BackupArgs
}

& $Python -m avidbackup @BackupArgs
$code = $LASTEXITCODE
Add-Content -Path (Join-Path $LogDir "scheduler.log") -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') END exit=$code"
exit $code
