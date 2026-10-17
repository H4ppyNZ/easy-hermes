# Registers a daily scheduled task that runs the NightlyRestartTask (hermes update,
# model selector, gateway restart, extra tasks). Follows the same pattern as
# Set-ModelSelectorTask.ps1: runs as the same user that owns the Hermes gateway
# (Interactive), NOT as SYSTEM, because Hermes lives in that user's profile.
#
# Usage:
#   .\Set-NightlyTask.ps1
#   .\Set-NightlyTask.ps1 -RunAt '3:00AM'
#   .\Set-NightlyTask.ps1 -HermesHome 'C:\Users\OtherUser\AppData\Local\hermes'

param(
    [string]$HermesUser = $env:USERNAME,
    [string]$RunAt = '3:00AM',
    [string]$HermesHome = $env:LOCALAPPDATA + '\hermes'
)

$ErrorActionPreference = 'Stop'

$taskName = 'Nightly Restart'
$taskScript = Join-Path $PSScriptRoot 'NightlyRestartTask.ps1'

if (-not (Test-Path -LiteralPath $taskScript -PathType Leaf)) {
    throw "Task script not found: $taskScript"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $HermesUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -TaskPath '\Hermes\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "Scheduled task '$taskName' will run '$taskScript' daily at $RunAt as '$HermesUser'."
Write-Output "NOTE: an Interactive-principal task only fires when '$HermesUser' is logged on."
Write-Output "      For unattended runs use -LogonType S4U with a stored credential."
