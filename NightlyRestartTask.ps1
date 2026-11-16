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
  4. Any extra tasks from extra-tasks.ps1 (gitignored, next to this script)
     and the -ExtraTasks parameter

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

.PARAMETER ExtraTasks
Extra script paths to run after the gateway restart. They are appended to the
tasks loaded from extra-tasks.ps1.

.PARAMETER ExtraTasksConfig
Path to the extra-tasks config file. Defaults to extra-tasks.ps1 next to this
script. That file is gitignored: host-specific paths belong there, never in the
repo. See docs/nightly-restart.md.

.EXAMPLE
.\NightlyRestartTask.ps1
Runs the full nightly sequence.

.EXAMPLE
.\NightlyRestartTask.ps1 -SkipUpdate -DryRun
Exercises the gate without updating Hermes or touching config.yaml.

.EXAMPLE
.\NightlyRestartTask.ps1 -ExtraTasks 'D:\Games\repoGaming\bedrockupdater.ps1'
Runs the nightly sequence plus one extra script.

.EXAMPLE
# extra-tasks.ps1, next to this script and gitignored:
#   @(
#     @{ Name = 'bedrock updater'; Command = { & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart } }
#   )
.\NightlyRestartTask.ps1
Loads the extra tasks from extra-tasks.ps1.
#>

param(
    [string]$Time = "05:00",
    [string]$HermesHome = "C:\Users\mattw\AppData\Local\hermes",
    [string[]]$ExtraTasks = @(),
    [string]$ExtraTasksConfig = '',
    [switch]$SkipUpdate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$eventSource = 'NightlyRestartTask'
$script:HadError = $false

# -- Child-process encoding -------------------------------------------------------------
# MUST be set before any python child. Without it the child inherits the console code page
# (cp1252) for its stdout/stderr pipes, and select-models.py's box-drawing characters
# (U+2500 in its PROMOTION GATE banner) raise UnicodeEncodeError at print time.
# Under PowerShell 5.1 that stderr traceback is additionally promoted to a
# NativeCommandError, so the `2>&1 | Out-String` call lands in the catch block, the real
# exit code is lost, and step 2 is reported as "Model chain update failed" before
# config.yaml is ever written. Verified: unset -> step 2 aborts; set -> run completes.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# -- Native-command helper --------------------------------------------------------------
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

# -- Extra tasks (step 4) ---------------------------------------------------------------
# Host-specific extras come from a gitignored extra-tasks.ps1 next to this script
# (override with -ExtraTasksConfig) and/or the -ExtraTasks parameter. Nothing is
# hardcoded, so a push never carries another host's paths.
#
# The config file is dot-sourced, so an entry's scriptblock can close over
# variables the file defines (e.g. one $tool path reused by several entries).
# Keep it to data and scriptblocks: it runs as the scheduled-task user.
#
# Accepted shapes (the file outputs exactly one of these):
#   array of entries:  @(
#       @{ Name = 'bedrock updater'; Command = { & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart } },
#       @{ Name = 'log cleanup'; Path = 'D:\scripts\cleanup.ps1'; Arguments = @('-KeepDays', 7) }
#   )
#   name -> entry map: @{ 'bedrock updater' = { & 'D:\...\bedrockupdater.ps1' -Restart } }
#   a single entry:    @{ Name = 'x'; Command = { ... } }
# Entry keys: Name (optional), Command (scriptblock) or Path (+ Arguments),
# Enabled (default $true). A hashtable carrying Command/Path is one entry; any
# other hashtable is treated as a name -> entry map.
function Get-ScriptblockTarget {
    # Best-effort literal path of the first command in a scriptblock. Returns
    # $null for a variable/expression target, which cannot be validated up front.
    param([scriptblock]$Command)
    $text = "$Command".Trim()
    if (-not $text) { return $null }
    if ($text.StartsWith('&')) { $text = $text.Substring(1).Trim() }
    if (-not $text) { return $null }
    $first = $text[0]
    if ($first -eq [char]39 -or $first -eq [char]34) {
        $end = $text.IndexOf($first, 1)
        if ($end -gt 1) { return $text.Substring(1, $end - 1) }
        return $null
    }
    if ($first -eq '$' -or $first -eq '(') { return $null }
    $match = [regex]::Match($text, '^\S+')
    if ($match.Success) { return $match.Value }
    return $null
}

function Test-LiteralPathTarget {
    # A target that is a variable or subexpression cannot be checked with
    # Test-Path; leave those to run and report a real failure.
    param([string]$Target)
    if ([string]::IsNullOrWhiteSpace($Target)) { return $false }
    return -not ($Target[0] -eq '$' -or $Target[0] -eq '(')
}

function Get-ExtraTasksConfigPath {
    # Default: extra-tasks.ps1 next to this script. -ExtraTasksConfig wins.
    # A relative override is resolved against the current directory.
    param(
        [string]$ExtraTasksConfig,
        [string]$ScriptDir
    )
    $path = if ($ExtraTasksConfig) { $ExtraTasksConfig } else { Join-Path $ScriptDir 'extra-tasks.ps1' }
    if ($path -and -not [System.IO.Path]::IsPathRooted($path)) {
        $path = Join-Path (Get-Location).Path $path
    }
    return $path
}

function Resolve-ExtraTasks {
    <#
    Normalizes config data (and the -ExtraTasks paths) into a list of
    @{ Name; Command; Path; Arguments; ConfigDir }. Disabled entries and entries
    with neither Command nor Path are dropped with a log line, so step 4 never
    aborts the nightly run.
    #>
    param(
        $Data,
        [string]$ConfigDir = '',
        [string[]]$ExtraTasks = @()
    )
    $tasks = @()
    $items = @()

    if ($null -ne $Data) {
        if ($Data -is [string] -or $Data -is [scriptblock]) {
            $items += ,@{ Name = $null; Entry = $Data }
        } elseif ($Data -is [System.Collections.IDictionary]) {
            if ($Data.Contains('Command') -or $Data.Contains('Path')) {
                $items += ,@{ Name = $null; Entry = $Data }
            } else {
                foreach ($key in @($Data.Keys)) {
                    $items += ,@{ Name = "$key"; Entry = $Data[$key] }
                }
            }
        } else {
            foreach ($entry in @($Data)) {
                $items += ,@{ Name = $null; Entry = $entry }
            }
        }
    }

    foreach ($item in $items) {
        $entry = $item.Entry
        $name = $item.Name
        $command = $null
        $path = $null
        $argList = @()
        $enabled = $true

        if ($entry -is [scriptblock]) {
            $command = $entry
        } elseif ($entry -is [string]) {
            $path = $entry
        } elseif ($entry -is [System.Collections.IDictionary]) {
            if ($null -ne $entry['Enabled']) { $enabled = [bool]$entry['Enabled'] }
            if ($entry['Name']) { $name = "$($entry['Name'])" }
            if ($entry['Command'] -is [scriptblock]) { $command = $entry['Command'] }
            if (-not $command -and $entry['Path']) { $path = "$($entry['Path'])" }
            if ($entry['Arguments']) { $argList = @($entry['Arguments']) | ForEach-Object { "$_" } }
        } else {
            Write-Log "Extra task entry of type $($entry.GetType().FullName) is not supported; skipped."
            continue
        }

        if (-not $name) {
            if ($path) { $name = Split-Path $path -Leaf } else { $name = 'extra task' }
        }
        if (-not $enabled) {
            Write-Log "Extra task '$name' is disabled; skipped."
            continue
        }
        if (-not $command -and -not $path) {
            Write-Log "Extra task '$name' has neither Command nor Path; skipped."
            continue
        }
        $tasks += @{
            Name      = $name
            Command   = $command
            Path      = $path
            Arguments = $argList
            ConfigDir = $ConfigDir
        }
    }

    foreach ($extra in @($ExtraTasks)) {
        if (-not $extra) { continue }
        $tasks += @{
            Name      = (Split-Path $extra -Leaf)
            Command   = $null
            Path      = "$extra"
            Arguments = @()
            ConfigDir = $ConfigDir
        }
    }
    return $tasks
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
$fallbackArgs = @($selectorPy, '--hermes-home', $HermesHome, '--full')
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
# Extras come from a gitignored extra-tasks.ps1 next to this script (override
# with -ExtraTasksConfig) and/or -ExtraTasks. Nothing host-specific is
# hardcoded here, so a push never carries another machine's paths.
$extraTasksPath = Get-ExtraTasksConfigPath -ExtraTasksConfig $ExtraTasksConfig -ScriptDir $PSScriptRoot
$extraTasksDir = if ($extraTasksPath) { Split-Path -Parent $extraTasksPath } else { $PSScriptRoot }

$configData = $null
if ($extraTasksPath -and (Test-Path -LiteralPath $extraTasksPath -PathType Leaf)) {
    Write-Log "Loading extra tasks from $extraTasksPath"
    try {
        # Dot-sourced at script scope so an entry's scriptblock can see
        # variables the config defines (see the note above Resolve-ExtraTasks).
        $configData = . $extraTasksPath
    } catch {
        Write-LogError "Extra tasks config failed to load: $extraTasksPath -- $_"
    }
} else {
    Write-Log "No extra tasks config at $extraTasksPath (step 4 uses -ExtraTasks only)."
}

function Invoke-ExtraTasks {
    <#
    Runs the normalized extra tasks. A task whose literal target is missing is
    skipped with a log line; a task that throws is logged as an error. Neither
    aborts the nightly run.
    #>
    param($Tasks)

    Write-Log "Step 4/4: Running $(@($Tasks).Count) additional task(s)..."
    $i = 0
    foreach ($task in @($Tasks)) {
        $i++
        # Skip a task whose literal target is missing instead of aborting the
        # run. A variable/expression target is not validated; it runs and a
        # real failure is caught below.
        $target = $null
        $pushLocation = $false
        $taskDir = if ($task.ConfigDir) { $task.ConfigDir } else { (Get-Location).Path }
        if ($task.Command) {
            $raw = Get-ScriptblockTarget -Command $task.Command
            if ($raw -and (Test-LiteralPathTarget -Target $raw)) { $target = $raw }
        } elseif ($task.Path) {
            $target = $task.Path
        }
        if ($target) {
            if (-not [System.IO.Path]::IsPathRooted($target)) {
                $target = Join-Path $taskDir $target
                $pushLocation = [bool]$task.Command
            }
            if (-not (Test-Path -LiteralPath $target)) {
                Write-Log "Task $i ($($task.Name)): SKIPPED (script not found: $target)"
                continue
            }
        }

        Write-Log "Invoking task $i/$(@($Tasks).Count): $($task.Name)"
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if ($pushLocation) { Push-Location -LiteralPath $taskDir }
            try {
                if ($task.Command) {
                    $output = & $task.Command 2>&1 | Out-String
                } else {
                    $output = & $task.Path @($task.Arguments) 2>&1 | Out-String
                }
                Write-Log "Task $i ($($task.Name)) output: $output"
            } finally {
                if ($pushLocation) { Pop-Location }
            }
        } catch {
            Write-LogError "Task $i ($($task.Name)) failed: $_"
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }
}

# @( ... ) is load-bearing: Resolve-ExtraTasks returns $null (not @()) when there are
# no tasks, because a bare `return $tasks` on an empty array emits zero pipeline
# items. Without the wrap, @($null).Count is 1, so step 4 "runs" one phantom task
# whose Command and Path are both null, and `& $task.Path` throws -- failing the
# whole nightly run. Verified: unwrapped -> "Task 1 () failed: The expression
# after '&' in a pipeline element produced an object that was not valid."
$otherTasks = @(Resolve-ExtraTasks -Data $configData -ConfigDir $extraTasksDir -ExtraTasks $ExtraTasks)
Invoke-ExtraTasks -Tasks $otherTasks

if ($script:HadError) {
    Write-Log "=== Nightly restart COMPLETED WITH ERRORS ==="
    exit 1
}
Write-Log "=== Nightly restart completed successfully ==="
exit 0
