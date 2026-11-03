param([Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
$destination = (Resolve-Path -LiteralPath $Target).Path
if ($destination -eq $PSScriptRoot) { throw 'Use a separate clone, not the active installation.' }
if (-not (Test-Path -LiteralPath (Join-Path $destination 'app.py') -PathType Leaf)) { throw 'Target must be an existing LocalOps installation.' }
if (netstat -ano -p tcp | Select-String ':8790\s+.*LISTENING\s+\d+') { throw 'Stop LocalOps on port 8790 before updating.' }
$backup = $destination + '-source-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
New-Item -ItemType Directory -Path $backup | Out-Null
$files = @('app.py','desktop_apps.py','schedules.py','fleet.py','requirements.txt')
foreach ($name in $files) {
    $existing = Join-Path $destination $name
    if (Test-Path -LiteralPath $existing) { Copy-Item -LiteralPath $existing -Destination $backup }
}
if (Test-Path -LiteralPath (Join-Path $destination 'static')) { Copy-Item -LiteralPath (Join-Path $destination 'static') -Destination $backup -Recurse }
foreach ($name in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $destination -Force }
Copy-Item -Path (Join-Path $PSScriptRoot 'static\*') -Destination (Join-Path $destination 'static') -Recurse -Force
Write-Host "Updated source at $destination. Backup: $backup"
Write-Host 'Review data/node-config.json and restart with this computer''s existing launcher.'

