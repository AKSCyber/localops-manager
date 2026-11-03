param([int]$OwnerPid, [switch]$Once)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root 'data\cpu-temperature.json'
$library = Join-Path $root 'vendor\LibreHardwareMonitor\LibreHardwareMonitorLib.dll'
$mutex = [System.Threading.Mutex]::new($false, 'Local\LocalOpsCpuSensors')
$owned = $false
$computer = $null
function Write-Sample($payload) {
    [System.IO.File]::WriteAllText("$output.tmp", ($payload | ConvertTo-Json -Depth 5))
    Move-Item -LiteralPath "$output.tmp" -Destination $output -Force
}
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
    Add-Type -Path $library
    do {
        try {
            if ($null -eq $computer) {
                if (-not [LibreHardwareMonitor.PawnIo.PawnIo]::IsInstalled) {
                    throw 'CPU temperature needs the signed PawnIO sensor driver. Install it; LocalOps will retry automatically.'
                }
                $computer = New-Object LibreHardwareMonitor.Hardware.Computer
                $computer.IsCpuEnabled = $true
                $computer.Open()
            }
            $rows = @()
            foreach ($hardware in $computer.Hardware) {
                $hardware.Update()
                foreach ($sensor in $hardware.Sensors) {
                    if ($sensor.SensorType -eq 'Temperature' -and $null -ne $sensor.Value) {
                        $rows += @{ name = $sensor.Name; celsius = [double]$sensor.Value; id = $sensor.Identifier.ToString() }
                    }
                }
            }
            if (-not $rows.Count) { throw 'The CPU temperature sensor could not be read by Libre Hardware Monitor. LocalOps will retry automatically; check that LocalOps is running as administrator.' }
            Write-Sample @{ sampledAt = [DateTimeOffset]::UtcNow.ToString('o'); sensors = @($rows); error = $null }
        } catch {
            $failure = $_.Exception.Message
            if ($computer) { try { $computer.Close() } catch {} }
            $computer = $null
            Write-Sample @{ sampledAt = [DateTimeOffset]::UtcNow.ToString('o'); sensors = @(); error = $failure }
        }
        if ($Once) { break }
        Start-Sleep -Seconds 5
    } while ($OwnerPid -gt 0 -and (Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue))
} finally {
    if ($computer) { $computer.Close() }
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
