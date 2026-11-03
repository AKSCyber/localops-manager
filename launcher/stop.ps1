param([switch]$Force)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $appRoot 'logs\localops.pid'
if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Host 'LocalOps has no recorded background PID.'
    exit 0
}

$savedPid = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) {
    throw 'The LocalOps PID file is invalid. No process was stopped.'
}
$process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host 'LocalOps was already stopped.'
    exit 0
}
& python.exe (Join-Path $PSScriptRoot 'verify-runtime.py') $savedPid $appRoot
if ($LASTEXITCODE -ne 0) {
    throw "PID $savedPid does not match the recorded LocalOps listener. No process was stopped."
}

Stop-Process -Id $savedPid -Force:$Force
if (-not $process.WaitForExit(5000) -and -not $Force) {
    throw 'LocalOps did not stop within five seconds. Re-run stop.ps1 with -Force only after rechecking the PID.'
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Write-Host 'LocalOps stopped.'
