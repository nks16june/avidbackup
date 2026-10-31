# Sequential multi-account backup (safer for a shared FTPS host than parallel).
param(
    [switch]$FilesOnly,
    [switch]$DbOnly,
    [string[]]$Account = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$extra = @()
if ($FilesOnly) { $extra += "--files-only" }
if ($DbOnly) { $extra += "--db-only" }

if ($Account.Count -eq 0) {
    & $Python -m avidbackup run @extra
    exit $LASTEXITCODE
}

Add-Content (Join-Path $LogDir "scheduler.log") "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') RUN_ALL start accounts=$($Account -join ',')"

$worst = 0
foreach ($id in $Account) {
    Write-Host "=== $id ==="
    & $Python -m avidbackup run --account $id @extra
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    Add-Content (Join-Path $LogDir "scheduler.log") "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') END account=$id exit=$code"
    if ($code -gt $worst) { $worst = $code }
}
exit $worst
