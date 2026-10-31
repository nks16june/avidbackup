# Register a daily Windows Scheduled Task for AvidBackup.
param(
    [string]$TaskName = "AvidBackup-Daily",
    [string]$Time = "02:00"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_backup.ps1"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Runner)) { throw "Missing runner: $Runner" }
if (-not (Test-Path $VenvPython)) { throw "Missing .venv — run scripts\install.ps1 first" }
if (-not (Test-Path (Join-Path $Root "accounts.json"))) {
    throw "Missing accounts.json — run: .\.venv\Scripts\avidbackup.exe init"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" run" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Host "Scheduled task '$TaskName' registered daily at $Time"
Write-Host "Root: $Root"
Write-Host "Uninstall: Unregister-ScheduledTask -TaskName $TaskName"
