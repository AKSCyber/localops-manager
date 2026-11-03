$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$statusPath = Join-Path $appRoot 'logs\startup-activation.json'
$result = [ordered]@{ completed = $false; elevated = $false; taskRunLevel = $null; error = $null; at = (Get-Date).ToString('o') }

try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'The activation script did not receive administrator rights.'
    }
    $listener = netstat -ano -p tcp | Select-String ':8790\s+.*LISTENING\s+(\d+)' | Select-Object -First 1
    if ($listener) {
        $processId = [int]$listener.Matches[0].Groups[1].Value
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
        if ($process.Name -ne 'python.exe' -or $process.CommandLine -notmatch 'app\.py') {
            throw "Port 8790 is owned by unexpected PID $processId and was left untouched."
        }
        taskkill.exe /F /PID $processId | Out-Null
    }
    & (Join-Path $PSScriptRoot 'install-startup.ps1') -StartNow
    $deadline = (Get-Date).AddSeconds(90)
    $bootstrap = $null
    do {
        Start-Sleep -Milliseconds 500
        try { $bootstrap = Invoke-RestMethod 'http://127.0.0.1:8790/api/bootstrap' -TimeoutSec 3 } catch { $bootstrap = $null }
    } until ($bootstrap.system.elevated -eq $true -or (Get-Date) -ge $deadline)
    if (-not $bootstrap -or $bootstrap.system.elevated -ne $true) {
        throw 'LocalOps did not return as an elevated healthy process within 90 seconds.'
    }
    $task = Get-ScheduledTask -TaskName 'LocalOps Manager'
    $result.completed = $true
    $result.elevated = $true
    $result.taskRunLevel = [string]$task.Principal.RunLevel
} catch {
    $result.error = $_.Exception.Message
} finally {
    $result | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

if (-not $result.completed) { exit 1 }
