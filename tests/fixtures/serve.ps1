param([string]$Python, [int]$Port)
$ErrorActionPreference = 'Stop'
& $Python -m http.server $Port --bind 127.0.0.1
