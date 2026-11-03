@echo off
setlocal
set "ACTIVATOR=%~dp0launcher\activate-elevated-startup.ps1"
set "RESULT=%~dp0logs\startup-activation.json"
echo LocalOps will request administrator approval once.
echo Approve the Windows UAC prompt to enable elevated application control at sign-in.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File ""%ACTIVATOR%""' -Wait"
if exist "%RESULT%" (
  type "%RESULT%"
) else (
  echo Activation did not produce a result. The UAC prompt may have been cancelled.
)
pause
endlocal
