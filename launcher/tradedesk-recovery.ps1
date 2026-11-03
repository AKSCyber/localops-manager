. 'D:\AI\Setup\Enable-AI.ps1'
param([ValidateSet('pause','resume')][string]$Action)
$ErrorActionPreference = 'Stop'
$stateFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'data\tradedesk-recovery-pause.json'
$task = Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq 'TradeDesk AI API Recovery' -and $_.TaskPath -eq '\' }
if (-not $task) { if (Test-Path $stateFile) { throw 'Previously paused TradeDesk recovery task is missing.' }; exit 0 }
$expected = 'D:\AI\Projects\Projects\tradedesk-ai\scripts\tradedesk-api-recovery.ps1'
if ($task.Actions.Count -ne 1 -or $task.Actions[0].Arguments -notmatch [regex]::Escape($expected)) {
    throw 'TradeDesk recovery task action changed; no task was modified.'
}
if ($Action -eq 'pause') {
    if (-not (Test-Path $stateFile)) {
        @{ enabled = [bool]$task.Settings.Enabled; running = ($task.State -eq 'Running'); at = (Get-Date).ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    }
    Disable-ScheduledTask -InputObject $task | Out-Null
    Stop-ScheduledTask -InputObject $task
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $current = Get-ScheduledTask -TaskName $task.TaskName
        if ($current.State -ne 'Running') { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($current.Settings.Enabled -or $current.State -eq 'Running') { throw 'TradeDesk recovery could not be paused; application stop aborted.' }
} elseif (Test-Path $stateFile) {
    $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    if ($saved.enabled) { Enable-ScheduledTask -InputObject $task | Out-Null }
    if ($saved.running -and $saved.enabled) { Start-ScheduledTask -InputObject $task }
    Remove-Item -LiteralPath $stateFile
}
