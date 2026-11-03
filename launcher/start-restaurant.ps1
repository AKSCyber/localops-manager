$ErrorActionPreference = 'Stop'
$appRoot = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'restaurant-pos-system'
$script = Join-Path $appRoot 'run-local.cmd'
if (-not (Test-Path -LiteralPath $script)) { throw 'Restaurant launcher is missing.' }
# Keep the existing database and canonical launcher; avoid Smart Home on 3000.
$env:PORT = '8781'
$env:STRICT_PORT = '1'
$env:BIND_HOST = '0.0.0.0'
Set-Location -LiteralPath $appRoot
& $script
exit $LASTEXITCODE
