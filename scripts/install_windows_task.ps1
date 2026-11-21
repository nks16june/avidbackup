# Register the nightly backup and the morning phone-status check.
param(
    [string]$TaskName = "AvidWeb-Daily-Backup",
    [string]$Time = "02:00",
    [string]$StatusTaskName = "AvidBackup-Morning-Status",
    [string]$StatusTime = "08:00"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_backup.ps1"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Runner)) { throw "Missing runner: $Runner" }
if (-not (Test-Path $VenvPython)) { throw "Missing .venv. Run scripts\install.ps1 first" }
if (-not (Test-Path (Join-Path $Root "accounts.json"))) {
    throw "Missing accounts.json. Run: .\.venv\Scripts\avidbackup.exe init"
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$BackupAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" run" `
    -WorkingDirectory $Root
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At $Time
Register-ScheduledTask -TaskName $TaskName -Action $BackupAction -Trigger $BackupTrigger -Settings $Settings -Principal $Principal -Force | Out-Null

$StatusAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" digest" `
    -WorkingDirectory $Root
$StatusTrigger = New-ScheduledTaskTrigger -Daily -At $StatusTime
$StatusSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $StatusTaskName -Action $StatusAction -Trigger $StatusTrigger -Settings $StatusSettings -Principal $Principal -Force | Out-Null

Write-Host "Nightly backup '$TaskName' at $Time"
Write-Host "Morning phone status '$StatusTaskName' at $StatusTime"
Write-Host "Root: $Root"
