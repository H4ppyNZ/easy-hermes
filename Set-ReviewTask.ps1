# Registers a daily scheduled task that reviews the Nightly Restart task.
# Runs after the nightly task (default 6:00AM).
#
# Usage:
#   .\Set-ReviewTask.ps1
#   .\Set-ReviewTask.ps1 -RunAt '6:30AM'

param(
    [string]$HermesUser = $env:USERNAME,
    [string]$RunAt = '6:00AM',
    [string]$HermesHome = $env:LOCALAPPDATA + '\hermes'
)

$ErrorActionPreference = 'Stop'

$taskName = 'Nightly Review'
$taskScript = Join-Path $PSScriptRoot 'ReviewNightlyTask.ps1'

if (-not (Test-Path -LiteralPath $taskScript -PathType Leaf)) {
    throw "Task script not found: $taskScript"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`" -HermesHome `"$HermesHome`""
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $HermesUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -TaskPath '\Hermes\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "Scheduled task '$taskName' will run '$taskScript' daily at $RunAt as '$HermesUser'."
