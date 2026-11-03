param([switch]$NoWait)

$ErrorActionPreference = 'Stop'
$cloudflared = 'D:\AI\Tools\Cloudflared\cloudflared.exe'
$config = 'D:\AI\Config\Cloudflare\new-machine.yml'
$metricsPort = 20243
$appRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $appRoot 'logs'
$mutex = [System.Threading.Mutex]::new($false, 'Local\LocalOpsCloudflareStart')
$ownsMutex = $false

function Get-MetricsOwner {
    $line = netstat -ano -p tcp | Select-String "127\.0\.0\.1:$metricsPort\s+.*LISTENING\s+(\d+)" | Select-Object -First 1
    if (-not $line) { return $null }
    return [int]$line.Matches[0].Groups[1].Value
}

function Test-NamedTunnel([int]$ProcessId) {
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
        return $process.Name -eq 'cloudflared.exe' -and $process.CommandLine -match [regex]::Escape($config)
    } catch { return $false }
}

try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another LocalOps tunnel start is already in progress.' }
    $owner = Get-MetricsOwner
    if ($owner) {
        if (Test-NamedTunnel $owner) { return }
        throw "Metrics port $metricsPort is occupied by an unrelated PID $owner. It was left untouched."
    }
    if (-not (Test-Path -LiteralPath $cloudflared -PathType Leaf)) { throw "cloudflared was not found at $cloudflared" }
    if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw "Tunnel config was not found at $config" }
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $arguments = @('--config', $config, '--metrics', "127.0.0.1:$metricsPort", 'tunnel', 'run')
    $process = Start-Process -FilePath $cloudflared -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logRoot 'cloudflared.out.log') `
        -RedirectStandardError (Join-Path $logRoot 'cloudflared.err.log') -PassThru
    if ($NoWait) { return }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $owner = Get-MetricsOwner
        if ($owner -and (Test-NamedTunnel $owner)) { return }
        if ($process.HasExited) {
            $detail = (Get-Content -LiteralPath (Join-Path $logRoot 'cloudflared.err.log') -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
            throw "cloudflared exited before becoming ready. $detail"
        }
        Start-Sleep -Milliseconds 500
    }
    throw "The named Cloudflare tunnel did not open metrics port $metricsPort within 30 seconds."
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
