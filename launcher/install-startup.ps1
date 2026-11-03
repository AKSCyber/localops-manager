param([switch]$StartNow, [switch]$Limited)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$localOpsRunLevel = if ($Limited) { 'Limited' } else { 'Highest' }
$localOpsPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel $localOpsRunLevel
$tunnelPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

$localOpsAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'start.ps1')`" -NoBrowser"
Register-ScheduledTask -TaskName 'LocalOps Manager' -Action $localOpsAction -Trigger $trigger -Principal $localOpsPrincipal -Settings $settings -Description 'Starts the protected LocalOps runtime command center elevated at Windows logon.' -Force | Out-Null

$tunnelAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'start-cloudflare.ps1')`""
Register-ScheduledTask -TaskName 'LocalOps Named Cloudflare Tunnel' -Action $tunnelAction -Trigger $trigger -Principal $tunnelPrincipal -Settings $settings -Description 'Starts the existing named Cloudflare tunnel once at Windows logon without duplicating its connector.' -Force | Out-Null

if ($StartNow) {
    Start-ScheduledTask -TaskName 'LocalOps Manager'
    Start-ScheduledTask -TaskName 'LocalOps Named Cloudflare Tunnel'
}
