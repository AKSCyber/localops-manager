$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$resultPath = Join-Path $root 'logs\runtime-update.json'
$result = [ordered]@{ at=(Get-Date).ToString('o'); completed=$false; version=$null; error=$null }
try {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator approval is required to restart elevated LocalOps.' }
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $task = $scheduler.GetFolder('\').GetTask('LocalOps Manager')
    $expected = Join-Path $PSScriptRoot 'start.ps1'
    $definition = $task.Definition
    if ($definition.Actions.Count -ne 1 -or $definition.Actions.Item(1).Arguments -notmatch [regex]::Escape($expected) -or $definition.Principal.RunLevel -ne 1) { throw 'LocalOps startup task no longer matches this installation.' }
    $health = Invoke-RestMethod 'http://127.0.0.1:8790/health' -TimeoutSec 5
    if ($health.app -ne 'LocalOps') { throw 'Port 8790 is not LocalOps.' }
    & (Join-Path $PSScriptRoot 'stop.ps1') -Force
    # Allow the owner-bound sensor reader to release its mutex.
    Start-Sleep -Seconds 6
    $task.Run($null) | Out-Null
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        try { $status = Invoke-RestMethod 'http://127.0.0.1:8790/api/runtime' -TimeoutSec 10 } catch { $status = $null }
    } until (($status.server.version -eq '1.6.0' -and $status.system.elevated) -or (Get-Date) -ge $deadline)
    if (-not $status -or $status.server.version -ne '1.6.0' -or -not $status.system.elevated) { throw 'Updated elevated LocalOps did not become ready.' }
    $registration = & python.exe (Join-Path $root 'register-extra-apps.py') 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('Additional application registration failed: ' + ($registration -join ' ')) }
    $result.version = $status.server.version
    $result.completed = $true
} catch { $result.error = $_.Exception.Message }
$result | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding utf8
if (-not $result.completed) { exit 1 }
