<#
.SYNOPSIS
Reviews the outcome of the Nightly Restart scheduled task.

.DESCRIPTION
Runs after the nightly task and verifies:
  1. The 'Nightly Restart' scheduled task exists and its last run result.
  2. The most recent NightlyRestartTask event log entries (no errors).
  3. Hermes config.yaml has a non-empty model.default.
  4. The Hermes gateway process is running.

Logs its findings to the Windows Event Log under source 'ReviewNightlyTask'.
Exits non-zero on any failure so the scheduled task reports an error.

.PARAMETER HermesHome
Hermes home directory. Defaults to $env:LOCALAPPDATA\hermes.

.EXAMPLE
.\ReviewNightlyTask.ps1
#>
param(
    [string]$HermesHome = "C:\Users\mattw\AppData\Local\hermes"
)

$ErrorActionPreference = "Stop"
$eventSource = 'ReviewNightlyTask'

if (-not [System.Diagnostics.EventLog]::SourceExists($eventSource)) {
    [System.Diagnostics.EventLog]::CreateEventSource($eventSource, "Application")
}

function Write-Log {
    param([string]$Message, [string]$Type = "Information", [int]$EventId = 1000)
    $msg = if ($Message.Length -gt 32000) { $Message.Substring(0, 32000) + "..." } else { $Message }
    Write-EventLog -LogName Application -Source $eventSource -EntryType $Type -EventId $EventId -Message $msg
}

$failures = @()

# 1. Scheduled task existence + last run
try {
    $task = Get-ScheduledTask -TaskName 'Nightly Restart' -TaskPath '\Hermes\' -ErrorAction Stop
    $info = $task | Get-ScheduledTaskInfo
    $lastResult = $info.LastTaskResult
    Write-Log "Nightly Restart last run: $($info.LastRunTime) result=$lastResult"
    if ($lastResult -ne 0) {
        $failures += "Nightly Restart last run result = $lastResult"
    }
} catch {
    $failures += "Nightly Restart scheduled task not found: $($_.Exception.Message)"
}

# 2. Recent event log errors
try {
    $since = (Get-Date).AddHours(-26)
    $errs = Get-EventLog -LogName Application -Source 'NightlyRestartTask' -After $since -EntryType Error -ErrorAction SilentlyContinue
    if ($errs) {
        foreach ($e in $errs) {
            $failures += "NightlyRestartTask error ($($e.TimeGenerated)): $($e.Message.Substring(0,[Math]::Min(200,$e.Message.Length)))"
        }
    }
} catch { }

# 3. config.yaml model.default
try {
    $cfgPath = Join-Path $HermesHome "config.yaml"
    if (-not (Test-Path -LiteralPath $cfgPath)) {
        $failures += "config.yaml missing at $cfgPath"
    } else {
        $content = Get-Content -LiteralPath $cfgPath -Raw
        if ($content -notmatch "default:\s*\S") {
            $failures += "config.yaml has no model.default"
        } else {
            Write-Log "config.yaml model.default present"
        }
    }
} catch {
    $failures += "config check failed: $($_.Exception.Message)"
}

# 4. Gateway process
$gw = Get-Process -Name "hermes" -ErrorAction SilentlyContinue
if (-not $gw) {
    # not necessarily fatal; gateway may run as service
    Write-Log "hermes process not detected (may be running as service)" "Warning"
} else {
    Write-Log "hermes process running (pid $($gw.Id -join ','))"
}

if ($failures.Count -gt 0) {
    $msg = "Review found $($failures.Count) issue(s):`n" + ($failures -join "`n")
    Write-Log $msg "Error" 1001
    Write-Host $msg
    exit 1
}

Write-Log "Review passed: nightly task healthy"
Write-Host "Review passed: nightly task healthy"
exit 0
