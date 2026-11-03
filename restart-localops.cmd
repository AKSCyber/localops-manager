@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -Command "$script = Join-Path (Get-Location) 'launcher\restart.ps1'; $p = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File ' + [char]34 + $script + [char]34) -Wait -PassThru; exit $p.ExitCode"
if errorlevel 1 (
  echo LocalOps restart was not completed. Check logs\runtime-update.json or approve the Windows UAC prompt when retrying.
  pause
)
