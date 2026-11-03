. 'D:\AI\Setup\Enable-AI.ps1'
$ErrorActionPreference = 'Stop'
# Dedicated port avoids the llama.cpp router on 8080. Serve the deployed app.
$appRoot = 'D:\AI\Projects\Projects\kindergarten-touch'
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'index.html'))) { throw 'Kindergarten app is missing.' }
Set-Location -LiteralPath $appRoot
python.exe -m http.server 8780 --bind 127.0.0.1 --directory $appRoot
exit $LASTEXITCODE
