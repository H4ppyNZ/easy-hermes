# Registers a daily scheduled task that runs the model selector (live probe of
# the free tier). Follows the same pattern as Set-ScheduledTask.ps1 in the
# nightlyrestart repo: runs as the same user that owns the Hermes gateway
# (Interactive), NOT as SYSTEM, because Hermes lives in that user's profile.
#
# The selector probes free models, applies the promotion gate, and rewrites
# model.default + fallback_providers in config.yaml. It is the same script that
# hermes-setup.ps1 runs interactively -- this just automates it nightly.
#
# Usage:
#   .\Set-ModelSelectorTask.ps1
#   .\Set-ModelSelectorTask.ps1 -RunAt '4:00AM'
#   .\Set-ModelSelectorTask.ps1 -HermesHome 'C:\Users\OtherUser\AppData\Local\hermes'

param(
    [string]$HermesUser = $env:USERNAME,
    [string]$RunAt = '4:00AM',
    [string]$HermesHome = $env:LOCALAPPDATA + '\hermes'
)

$ErrorActionPreference = 'Stop'

$taskName = 'Hermes Model Selector'
$selectorScript = Join-Path $PSScriptRoot 'select-models.py'

if (-not (Test-Path -LiteralPath $selectorScript -PathType Leaf)) {
    throw "Selector script not found: $selectorScript"
}

# Resolve the venv python (same logic as hermes-setup.ps1)
$pythonPath = Join-Path $HermesHome 'hermes-agent\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    # Fallback: try to find it relative to the hermes command
    $hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermesCmd -and $hermesCmd.Source) {
        $hermesDir = Split-Path $hermesCmd.Source -Parent
        $pythonPath = Join-Path $hermesDir 'python.exe'
    }
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "venv python not found at '$pythonPath'. Install hermes first."
}

# Ensure pyyaml is available (the selector imports yaml)
$yamlCheck = & $pythonPath -c "import yaml" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Output "Installing pyyaml into the venv..."
    & $pythonPath -m pip install pyyaml 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install pyyaml."
    }
}

# Build the command: python selector.py --hermes-home <path>
# We use a small wrapper script so the scheduled task has a single file to
# execute and we can set PYTHONUTF8 before python launches.
$wrapperScript = Join-Path $PSScriptRoot 'Run-ModelSelectorTask.ps1'
$wrapperContent = @"
`$env:PYTHONUTF8 = '1'
`$env:PYTHONIOENCODING = 'utf-8'
& '$pythonPath' '$selectorScript' --hermes-home '$HermesHome' --full `@args
exit `$LASTEXITCODE
"@

# Only rewrite if content differs (avoid touching mtime on every registration)
$existing = if (Test-Path -LiteralPath $wrapperScript) { Get-Content -LiteralPath $wrapperScript -Raw } else { '' }
if ($existing -ne $wrapperContent) {
    Set-Content -LiteralPath $wrapperScript -Value $wrapperContent -Encoding UTF8
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $HermesUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -TaskPath '\Hermes\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "Scheduled task '$taskName' will run '$wrapperScript' daily at $RunAt as '$HermesUser'."
Write-Output "NOTE: an Interactive-principal task only fires when '$HermesUser' is logged on."
Write-Output "      For unattended runs use -LogonType S4U with a stored credential."
