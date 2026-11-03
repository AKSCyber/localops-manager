param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
. 'D:\AI\Setup\Enable-AI.ps1'
$appRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $appRoot 'logs'
$pidFile = Join-Path $logRoot 'localops.pid'
$healthUrl = 'http://127.0.0.1:8790/health'
$mutex = [System.Threading.Mutex]::new($false, 'Local\LocalOpsManagerStart')
$ownsMutex = $false

function Test-LocalOps {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        return $response.status -eq 'ok' -and $response.app -eq 'LocalOps'
    } catch { return $false }
}

function Test-PortOccupied {
    return $null -ne (netstat -ano -p tcp | Select-String ':8790\s+.*LISTENING\s+\d+' | Select-Object -First 1)
}

try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another LocalOps start is already in progress.' }

    if (-not (Test-LocalOps)) {
        if (Test-PortOccupied) { throw 'Port 8790 is occupied by a service that is not LocalOps. It was left untouched.' }
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $python = Join-Path $appRoot '.venv\Scripts\python.exe'
        $arguments = @('app.py')
        if (-not $python) {
            $python = (Get-Command py.exe -ErrorAction Stop).Source
            $arguments = @('-3', 'app.py')
        }
        $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $appRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logRoot 'localops.out.log') `
            -RedirectStandardError (Join-Path $logRoot 'localops.err.log') -PassThru
        Set-Content -LiteralPath $pidFile -Value $process.Id
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-LocalOps) -and (Get-Date) -lt $deadline) {
            if ($process.HasExited) {
                $detail = (Get-Content -LiteralPath (Join-Path $logRoot 'localops.err.log') -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
                throw "LocalOps exited before becoming ready. $detail"
            }
            Start-Sleep -Milliseconds 400
        }
        if (-not (Test-LocalOps)) { throw 'LocalOps did not become healthy within 30 seconds. Check logs\localops.err.log.' }
    }

    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8790' }
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
