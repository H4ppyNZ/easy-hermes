<#
.SYNOPSIS
Nightly restart task: updates Hermes, updates the fallback/default model chain,
restarts the gateway, then runs additional maintenance.

.DESCRIPTION
Runs, in order:
  1. hermes update --no-gateway-restart --yes
  2. select-models.py  (probe free models, apply the promotion gate, rewrite
     model.default + fallback_providers in config.yaml)
  3. hermes gateway restart  (so the new model/default is picked up)
  4. Any extra tasks listed in $otherTasks

Everything is logged to the Windows Event Log under source 'NightlyRestartTask'.

DESIGN NOTES (why it is written this way)
  * Step 2 runs BEFORE step 3 on purpose. The gateway must restart AFTER
    config.yaml is rewritten, or the new default does not take effect until the
    following night.
  * The script path lives under the Hermes home, NOT under cache/scratch/
    (which is pruned after 24h idle) and not in a repo checkout.
  * Write-EventLog truncates messages to 32766 bytes; a long probe run easily
    exceeds that and throws. Write-Log clamps every message.
  * 'hermes' is invoked as an .exe, not the .cmd shim: a .cmd cannot be run
    non-interactively by a scheduled task without cmd.exe in the pipeline, and
    PowerShell does not resolve 'hermes' to it reliably when the window is
    hidden.
  * The scheduled task must run whether or not the user is logged on, and
    hermes update requires an interactive-ish environment; see -RunAsSystem.

.PARAMETER Time
Time at which the scheduled task should run (used only when registering).

.PARAMETER HermesHome
Hermes home directory. Defaults to $env:LOCALAPPDATA\hermes.

.PARAMETER SkipUpdate
Skip step 1 (hermes update). Useful for testing the model refresh alone.

.PARAMETER DryRun
Pass --dry-run to select-models.py: report the gate decision and write nothing.

.EXAMPLE
.\NightlyRestartTask.ps1
Runs the full nightly sequence.

.EXAMPLE
.\NightlyRestartTask.ps1 -SkipUpdate -DryRun
Exercises the gate without updating Hermes or touching config.yaml.
#>

param(
    [string]$Time = "05:00",
    [string]$HermesHome = "C:\Users\mattw\AppData\Local\hermes",
    [string[]]$ExtraTasks = @(),
    [switch]$SkipUpdate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$eventSource = 'NightlyRestartTask'
$script:HadError = $false

# ── Child-process encoding ─────────────────────────────────────────────────────────────
# MUST be set before any python child. Without it the child inherits the console code page
# (cp1252) for its stdout/stderr pipes, and select-models.py's box-drawing characters
# (U+2500 in its PROMOTION GATE banner) raise UnicodeEncodeError at print time.
# Under PowerShell 5.1 that stderr traceback is additionally promoted to a
# NativeCommandError, so the `2>&1 | Out-String` call lands in the catch block, the real
# exit code is lost, and step 2 is reported as "Model chain update failed" before
# config.yaml is ever written. Verified: unset -> step 2 aborts; set -> run completes.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# ── Native-command helper ──────────────────────────────────────────────────────────────
# $ErrorActionPreference='Stop' + PS 5.1 turns ANY stderr byte from a native process into a
# terminating NativeCommandError, even when the process succeeded. That both loses the real
# exit code ($LASTEXITCODE reads -1 in the catch) and truncates the error to the first line
# of the traceback. Run natives with the preference relaxed and judge them by exit code.
function Invoke-Native {
    param(
        [string]$Exe,
        [string[]]$Arguments
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    return [pscustomobject]@{ Output = $output; ExitCode = $code }
}

# HermesHome is an EXPLICIT absolute path, not "$env:LOCALAPPDATA\hermes" on purpose.
# If this task is ever registered to run as another principal (SYSTEM, a service
# account), $env:LOCALAPPDATA / $env:USERPROFILE resolve to THAT account's profile and
# the script would silently target a nonexistent Hermes home. The explicit path plus the
# validation below makes a wrong principal fail loudly instead of doing nothing.
# See Set-ScheduledTask.ps1: the task must run as the same user that owns the gateway.

# Event Log message body is limited to 32766 bytes; longer messages throw.
# (Clamp below at 30000 to stay safely under the limit.)
$EventLogMaxChars = 30000

if (-not [System.Diagnostics.EventLog]::SourceExists($eventSource)) {
    try {
        New-EventLog -LogName Application -Source $eventSource -ErrorAction Stop
    } catch {
        # Not fatal: Write-EventLog below will still work if the source already exists.
        Write-Warning "Could not create event source '$eventSource': $_"
    }
}

function Write-Log {
    param(
        [string]$Message,
        [string]$EntryType = "Information"
    )
    Write-Output $Message
    $safe = $Message
    if ($safe.Length -gt $EventLogMaxChars) {
        $safe = $safe.Substring(0, $EventLogMaxChars) +
                "`n...[truncated $($Message.Length - $EventLogMaxChars) chars for Event Log]"
    }
    try {
        Write-EventLog -LogName Application -Source $eventSource -EventId 1000 `
            -EntryType $EntryType -Message $safe -ErrorAction Stop
    } catch {
        Write-Warning "Write-EventLog failed: $_"
    }
}

function Write-LogError {
    param([string]$Message)
    $script:HadError = $true
    Write-Log -Message $Message -EntryType Error
}

# -- Resolve paths (fail loudly rather than silently doing nothing) --
$pythonPath   = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$selectorPy   = Join-Path $PSScriptRoot 'select-models.py'
$hermesExe    = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"

# All three must exist: python runs the selector, that script is the
# model-chain step itself, and hermes.exe drives steps 1 and 3. A missing
# hermes.exe previously slipped through here and only surfaced as a failed
# step 1/step 3 later in the run.
foreach ($p in @($pythonPath, $selectorPy, $hermesExe)) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-LogError "Required path missing: $p"
        throw "Nightly restart aborted: $p not found"
    }
}

Write-Log "=== Nightly restart starting (HermesHome=$HermesHome) ==="

# -- Step 1: Update Hermes (without gateway restart) --
if ($SkipUpdate) {
    Write-Log "Step 1/4: SKIPPED (hermes update) - -SkipUpdate"
} else {
    Write-Log "Step 1/4: Starting Hermes update..."
    $r = Invoke-Native -Exe $hermesExe -Arguments @('update', '--no-gateway-restart', '--yes')
    Write-Log "Hermes update output: $($r.Output)"
    if ($r.ExitCode -ne 0) {
        Write-LogError "hermes update exited with code $($r.ExitCode)"
    }
}

# -- Step 2: Refresh free-model chain + apply the promotion gate --
Write-Log "Step 2/4: Updating fallback/default model chain..."
$fallbackArgs = @($selectorPy, '--hermes-home', $HermesHome)
if ($DryRun) { $fallbackArgs += "--dry-run" }
$r = Invoke-Native -Exe $pythonPath -Arguments $fallbackArgs
Write-Log "Model chain update output: $($r.Output)"

# select-models.py exits non-zero on a real failure; surface it as an error.
if ($r.ExitCode -ne 0) {
    Write-LogError "select-models.py exited with code $($r.ExitCode)"
}

# -- Step 3: Restart gateway (AFTER config.yaml is rewritten) --
if ($DryRun) {
    Write-Log "Step 3/4: SKIPPED gateway restart (dry run - config was not written)"
} else {
    Write-Log "Step 3/4: Restarting Hermes gateway..."
    $r = Invoke-Native -Exe $hermesExe -Arguments @('gateway', 'restart')
    Write-Log "Gateway restart output: $($r.Output)"
    if ($r.ExitCode -ne 0) {
        Write-LogError "hermes gateway restart exited with code $($r.ExitCode)"
    }
}

# -- Step 4: Other tasks --
# Host-specific extra tasks are NOT hardcoded here. Pass them via -ExtraTasks
# (array of script paths) or populate $otherTasks below; missing scripts are
# skipped rather than aborting the run.
$otherTasks = @()
if ($ExtraTasks) {
    foreach ($t in $ExtraTasks) {
        $otherTasks += @{ Name = (Split-Path $t -Leaf); Command = { & $t } }
    }
}

Write-Log "Step 4/4: Running $($otherTasks.Count) additional task(s)..."
$i = 0
foreach ($task in $otherTasks) {
    $i++
    # Basic path validation for script-based tasks.
    $target = $null
    try { $target = ($task.Command.ToString() -replace '.*&\s*', '').Trim().Trim("'\"") } catch { }
    if ($target -and -not (Test-Path -LiteralPath $target)) {
        Write-Log "Task $i ($($task.Name)): SKIPPED (script not found: $target)"
        continue
    }
    Write-Log "Invoking task $i/$($otherTasks.Count): $($task.Name)"
    try {
        $output = & $task.Command 2>&1 | Out-String
        Write-Log "Task $i ($($task.Name)) output: $output"
    } catch {
        Write-LogError "Task $i ($($task.Name)) failed: $_"
    }
}

if ($script:HadError) {
    Write-Log "=== Nightly restart COMPLETED WITH ERRORS ==="
    exit 1
}
Write-Log "=== Nightly restart completed successfully ==="
exit 0
