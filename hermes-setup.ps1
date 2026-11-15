<#
.SYNOPSIS
    Hermes Agent installer + model selector. Two modes, one entry point.

.DESCRIPTION
    -Yolo    Unattended. Installs Hermes, authenticates (Nous Portal OAuth,
              or API-key paste if declined), runs the model selector (a live
              probe of the free tier: picks the default model, refreshes the
              fallback chain and starts the promotion history), then prints
              a first-run primer. One interaction only.
    -Guided  Prompts: provider, model, live selector, optional ACP extras.
              Explains every choice, then the same tail as yolo.

    The model selector is a plain file -- select-models.py, shipped
    next to this script and run with the Hermes venv python. It is the single
    source of truth for the free-tier chain and for model.default; there is no
    hardcoded model list. When this script is piped through 'irm | iex' and no
    local copy exists, it is fetched from $SelectorUrl instead.

    Runs on Windows (native). For Linux/macOS/WSL2 use the official
    installer: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

    After install these always work:
      hermes chat            # interactive chat (Ctrl+C exits; /help in-session)
      hermes chat -q "..."   # one-shot answer
      hermes model           # change provider/model
      hermes setup           # reconfigure interactively
      hermes setup --portal  # re-auth Nous Portal
      hermes doctor          # health check

.NOTES
    Idempotent: re-running only fills gaps. ASCII-only source so it parses
    cleanly under Windows PowerShell 5.1, which is what iex uses. The model
    selector is a separate file (select-models.py) and may contain
    non-ASCII -- it is run by python, not parsed by PowerShell.

    Usage:
      .\hermes-setup.ps1 -Yolo
      .\hermes-setup.ps1 -Yolo -Model inclusionai/ling-3.1-flash
      .\hermes-setup.ps1              # guided
#>
[CmdletBinding()]
param(
    [switch]$Yolo,
    [string]$Model,
    [switch]$SkipInstall,
    [switch]$SkipDocs,
    [switch]$SelectorOnly,
    [switch]$SchedulerOnly,
    [switch]$Update,
    [switch]$NoUpdate
)

$ErrorActionPreference = 'Stop'
$ScriptVersion = '4'

# --- helpers ---------------------------------------------------------------
function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    [ok] $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }
function Write-Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }

# --- 0. execution policy check ----------------------------------------------
# Report (don't change) the effective ExecutionPolicy. The irm | iex one-liner
# works regardless (memory-based, not file-based), but a friend who downloads
# the .ps1 and runs it locally will hit Restricted policy. Flag it early.
$execPolicy = Get-ExecutionPolicy
if ($execPolicy -eq 'Bypass') {
    Write-Warn "ExecutionPolicy is Bypass -- any script runs without restriction."
    Write-Warn "Consider: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
} elseif ($execPolicy -eq 'Unrestricted') {
    Write-Warn "ExecutionPolicy is Unrestricted -- all scripts run, even untrusted."
    Write-Warn "Consider: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
} elseif ($execPolicy -eq 'Restricted') {
    Write-Warn "ExecutionPolicy is Restricted -- local .ps1 files are blocked."
    Write-Warn "The irm | iex one-liner still works (runs from memory)."
} elseif ($execPolicy -eq 'AllSigned') {
    Write-Info "ExecutionPolicy is AllSigned -- only signed scripts run."
    Write-Info "This script is unsigned; the irm | iex one-liner is the way to run it."
} else {
    Write-Info "ExecutionPolicy: $execPolicy"
}

function Test-Hermes {
    return $null -ne (Get-Command hermes -ErrorAction SilentlyContinue)
}

function Invoke-Hermes {
    # NOTE: never name this parameter $Args -- PowerShell special-cases the
    # automatic $args variable and a parameter by that name never binds,
    # silently turning every call into a bare `hermes` (interactive chat).
    param([string[]]$CmdArgs)
    # Native commands can write to stderr on success; under
    # ErrorActionPreference=Stop that trips a NativeCommandError. Capture
    # both streams without the error record.
    $out = & hermes @CmdArgs 2>&1
    return ($out | Out-String)
}

function Invoke-Native {
    param([string]$Exe, [string[]]$CmdArgs)
    # Stream a native command straight to the host and survive stderr.
    # Under ErrorActionPreference=Stop, a native command writing to stderr
    # raises a terminating NativeCommandError. Relax the preference, merge
    # streams, write to the host, and hand back the exit code. (Same
    # $CmdArgs rule as Invoke-Hermes: never $Args.)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @CmdArgs 2>&1 | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Hermes home (the data dir config.yaml, auth.json and caches live in).
# HERMES_HOME wins; otherwise the Windows default.
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }

# --- Where the scripts live, and how they get updated -----------------------
# Two install shapes, and they update differently:
#   * clone     -- a git work tree on disk. Authoritative; updated with git.
#   * one-liner -- 'irm | iex' / a scriptblock cast. $PSScriptRoot is EMPTY in
#                  this mode, so there is no directory to read sibling files
#                  from; the installer syncs the file set from the repo into a
#                  stable install dir instead.
# The previous code assumed a sibling file always existed and preferred it over
# a fresh download, so a stale copy was used forever.
$RepoOwner   = 'H4ppyNZ'
$RepoName    = 'easy-hermes'
$RepoBranch  = 'main'
# EASY_HERMES_RAW_BASE overrides the download root (used by the test suite to
# point at a local mirror; not something a user needs to set).
$RepoRawBase = if ($env:EASY_HERMES_RAW_BASE) { $env:EASY_HERMES_RAW_BASE } else {
    "https://raw.githubusercontent.com/$RepoOwner/$RepoName/$RepoBranch" }
$InstallDir  = Join-Path $HermesHome 'easy-hermes'
$NoUpdateMarker = Join-Path $InstallDir 'NOUPDATE'

function Resolve-ScriptBaseDir {
    # A git work tree next to this script is the user's own checkout: use it
    # as-is (Invoke-Update fast-forwards it). Anything else is an install, and
    # the managed copy under $InstallDir is used.
    if ($PSScriptRoot) {
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.git')) { return $PSScriptRoot }
    }
    return $InstallDir
}

function Invoke-Git {
    # git writes progress to stderr; under ErrorActionPreference=Stop that is a
    # terminating NativeCommandError. Relax the preference, judge by exit code.
    param([string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git @GitArgs 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ Output = $out; ExitCode = $code }
}

function Test-UpdateDeclined {
    if ($env:EASY_HERMES_NO_UPDATE -eq '1') { return $true }
    if (Test-Path -LiteralPath $NoUpdateMarker -PathType Leaf) { return $true }
    return $false
}

function Sync-InstallFiles {
    # Fetch MANIFEST.json, then every file it lists, into $InstallDir. Files are
    # compared by SHA256 rather than re-read as text: select-models.py contains
    # non-ASCII box characters that PowerShell 5.1 would decode as ANSI and
    # corrupt.
    param([string]$Dir)

    $manifestUrl = "$RepoRawBase/MANIFEST.json"
    $tmpManifest = Join-Path $env:TEMP ("easy-hermes-manifest-" + [guid]::NewGuid().ToString('N') + ".json")
    try {
        try {
            Invoke-WebRequest -Uri $manifestUrl -OutFile $tmpManifest -UseBasicParsing -ErrorAction Stop
            $manifest = Get-Content -LiteralPath $tmpManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            throw "Could not fetch $manifestUrl : $($_.Exception.Message)"
        }

        $relFiles = @($manifest.python) + @($manifest.powershell) + @($manifest.data)
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null

        $fetched = 0
        $changed = 0
        foreach ($rel in $relFiles) {
            $rel = $rel.Replace('/', [char]92)
            $dest = Join-Path $Dir $rel
            $parent = Split-Path $dest -Parent
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            $url = "$RepoRawBase/" + $rel.Replace([char]92, '/')
            $tmpFile = "$dest.__sync__"
            try {
                Invoke-WebRequest -Uri $url -OutFile $tmpFile -UseBasicParsing -ErrorAction Stop
            } catch {
                Write-Warn "fetch failed, keeping existing copy: $url ($($_.Exception.Message))"
                Remove-Item -LiteralPath $tmpFile -ErrorAction SilentlyContinue
                continue
            }
            $same = $false
            if (Test-Path -LiteralPath $dest -PathType Leaf) {
                $h1 = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
                $h2 = (Get-FileHash -LiteralPath $tmpFile -Algorithm SHA256).Hash
                $same = ($h1 -eq $h2)
            }
            if ($same) {
                Remove-Item -LiteralPath $tmpFile -ErrorAction SilentlyContinue
            } else {
                Move-Item -LiteralPath $tmpFile -Destination $dest -Force
                $changed++
            }
            $fetched++
        }
        [System.IO.File]::WriteAllText((Join-Path $Dir 'VERSION'), "$($manifest.version)`n")
        Write-Ok "synced $fetched file(s) into $Dir ($changed changed), version $($manifest.version)"
    } finally {
        Remove-Item -LiteralPath $tmpManifest -ErrorAction SilentlyContinue
    }
}

function Update-RepoClone {
    param([string]$Dir)

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Warn "git not found; cannot update $Dir"
        return $false
    }
    $r = Invoke-Git @('-C', $Dir, 'status', '--porcelain')
    if ($r.ExitCode -ne 0) { Write-Warn "git status failed in $Dir; skipping update"; return $false }
    if ($r.Output.Trim()) {
        Write-Warn "local changes in $Dir -- skipping update (commit or stash them first)"
        return $false
    }
    $upstream = Invoke-Git @('-C', $Dir, 'rev-parse', '--abbrev-ref', '@{u}')
    if ($upstream.ExitCode -ne 0 -or -not $upstream.Output.Trim()) {
        Write-Warn "no upstream tracking branch for $Dir; skipping update"
        return $false
    }
    $r = Invoke-Git @('-C', $Dir, 'fetch', '--quiet')
    if ($r.ExitCode -ne 0) { Write-Warn "git fetch failed for $Dir; skipping update"; return $false }

    $behind = (Invoke-Git @('-C', $Dir, 'rev-list', '--count', 'HEAD..@{u}')).Output
    $count = 0
    [void][int]::TryParse((($behind -replace '\D', '').Trim()), [ref]$count)
    if ($count -eq 0) {
        Write-Ok "$Dir is already up to date"
        return $true
    }

    $before = (Invoke-Git @('-C', $Dir, 'rev-parse', 'HEAD')).Output.Trim()
    $r = Invoke-Git @('-C', $Dir, 'merge', '--ff-only', '@{u}')
    if ($r.ExitCode -ne 0) {
        Write-Warn "git merge --ff-only failed in $Dir; leaving it as it was"
        return $false
    }
    $after = (Invoke-Git @('-C', $Dir, 'rev-parse', 'HEAD')).Output.Trim()
    Write-Ok ("updated {0} -> {1}" -f $before.Substring(0, 7), $after.Substring(0, 7))
    $log = (Invoke-Git @('-C', $Dir, 'log', '--oneline', "$before..$after")).Output
    foreach ($line in ($log -split "`n")) {
        if ($line.Trim()) { Write-Info "  $($line.Trim())" }
    }
    return $true
}

function Invoke-Update {
    <#
    Auto-update policy:
      -Yolo or -Update   -> update
      -NoUpdate          -> never update, and persist that choice
      marker / env var   -> never update (sticky opt-out)
      anything else      -> no update
    The opt-out is sticky so you do not have to re-pass a switch every run.
    #>
    if ($NoUpdate) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        [System.IO.File]::WriteAllText($NoUpdateMarker, "auto-update disabled by -NoUpdate $(Get-Date -Format o)`n")
        Write-Info "auto-update disabled (marker: $NoUpdateMarker). Delete it, or pass -Update, to re-enable."
        return
    }
    if (Test-UpdateDeclined) {
        Write-Info "auto-update is off (marker file or EASY_HERMES_NO_UPDATE=1); skipping."
        return
    }
    if (-not ($Yolo -or $Update)) {
        Write-Info "auto-update: not enabled for this run (use -Yolo or -Update)."
        return
    }
    if ($ScriptBaseDir -eq $InstallDir) {
        Write-Step "Syncing installer files"
        Sync-InstallFiles -Dir $InstallDir
    } else {
        Write-Step "Updating checkout"
        Update-RepoClone -Dir $ScriptBaseDir
    }
}

function Register-ScheduledScripts {
    <#
    One owner of the model chain: NightlyRestartTask.ps1 runs the selector as
    its step 2. The old standalone 'Hermes Model Selector' task is not
    registered -- two tasks probing the free tier would feed the promotion gate
    duplicate observations. The Hermes cron monitor replaced the separate
    ReviewNightlyTask.ps1 health check.
    #>
    $registrations = @(
        @{ Script = 'Set-NightlyTask.ps1';        Args = @('-HermesHome', $HermesHome) },
        @{ Script = 'Set-HermesCronMonitor.ps1'; Args = @() }
    )
    foreach ($reg in $registrations) {
        $scriptPath = Join-Path $ScriptBaseDir $reg.Script
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            Write-Warn "skipping missing script: $scriptPath"
            continue
        }
        try {
            & $scriptPath @($reg.Args)
            Write-Ok "registered: $($reg.Script)"
        } catch {
            Write-Warn "failed to register $($reg.Script): $($_.Exception.Message)"
        }
    }
}

function Get-SelectorScript {
    $path = Join-Path $ScriptBaseDir 'select-models.py'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "select-models.py not found in '$ScriptBaseDir'. Re-run without -NoUpdate so the installer can sync it from $RepoRawBase."
    }
    Write-Ok "selector: $path"
    return $path
}

# Resolve once, before any block that needs a script path. An unguarded
# 'Join-Path $PSScriptRoot ...' throws under the one-liner because
# $PSScriptRoot is empty there.
$ScriptBaseDir = Resolve-ScriptBaseDir
Invoke-Update

# --- SchedulerOnly: register the nightly job + monitor, then exit ------------
# The nightly job is the single owner of the model-chain refresh (step 2), so
# it is what -SchedulerOnly registers. The old standalone selector task is not
# registered here: two tasks both running select-models.py would probe the free
# tier twice a night and feed the promotion gate duplicate observations.
if ($SchedulerOnly) {
    Register-ScheduledScripts
    return
}

function Get-CurrentDefault {
    # 'hermes config get' exits 1 and writes to stderr when the key is unset;
    # under ErrorActionPreference=Stop that surfaces as a terminating
    # NativeCommandError, so a virgin config would kill the script. Trap it.
    try { return (Invoke-Hermes @('config', 'get', 'model.default')).Trim() } catch { return '' }
}

# --- SelectorOnly: skip install/auth, just run the selector ------------------
if ($SelectorOnly) {
    if (-not (Test-Hermes)) { throw "hermes not on PATH. Install it first or run without -SelectorOnly." }
    $runSelector = $true
    $SkipInstall = $true
    $SkipDocs = $true
} else {

# --- 1. platform ------------------------------------------------------------
Write-Step "Platform check"
Write-Info "OS: $([Environment]::OSVersion.VersionString)  arch: $env:PROCESSOR_ARCHITECTURE"

# --- 2. install -------------------------------------------------------------
if ($SkipInstall) {
    Write-Step "Skipping install"
    if (-not (Test-Hermes)) { throw "hermes not on PATH and -SkipInstall was passed." }
    Write-Ok "hermes present"
} else {
    Write-Step "Installing Hermes Agent"
    # Wrapper, not rewrite: drive the official installer, which handles its
    # own prerequisites, verification and receipts.
    $installer = "$env:TEMP\hermes-install.ps1"
    Write-Info "Downloading https://hermes-agent.nousresearch.com/install.ps1"
    Invoke-WebRequest -Uri "https://hermes-agent.nousresearch.com/install.ps1" -OutFile $installer -UseBasicParsing
    $installArgs = @{}
    if ($Yolo) { $installArgs.NonInteractive = $true }
    & $installer @installArgs
    # Refresh PATH in this process so the rest of the script can call hermes.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (Test-Hermes)) {
        Write-Warn "hermes is not on PATH yet. Open a new terminal and re-run this script with -SkipInstall."
        return
    }
    Write-Ok (Invoke-Hermes @('--version'))
}

# --- 3. auth ----------------------------------------------------------------
$nousLogged = $false
try {
    $authOut = Invoke-Hermes @('auth', 'list')
    if ($authOut -match 'nous') { $nousLogged = $true }
} catch { $nousLogged = $false }

if ($Yolo) {
    Write-Step "Authenticate (the one interaction)"
    if ($nousLogged) {
        Write-Ok "Nous Portal already authenticated"
    } else {
        Write-Info "A browser window will open for Nous Portal OAuth."
        Write-Info "No browser? Answer n below and paste an API key instead."
        $usePortal = Read-Host "Use Nous Portal login? [Y/n]"
        if ($usePortal -eq 'n') {
            $key = Read-Host "Paste your API key" -AsSecureString
            # Store into .env, never config.yaml; never echo the value.
            $envPath = (Invoke-Hermes @('config', 'env-path')).Trim()
            if (-not $envPath) { throw "could not resolve .env path" }
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key)
            try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
            Add-Content -Path $envPath -Value "NOUS_API_KEY=$plain"
            Write-Ok "key written to $envPath (not config.yaml, never echoed)"
        } else {
            # Streamed: the OAuth device code and browser prompt must be
            # visible while it runs.
            Invoke-Native hermes @('setup', '--portal')
        }
    }
} else {
    Write-Step "Choose a provider"
    Write-Info "  1. Nous Portal   one OAuth login, 300+ models, 10% off token providers (recommended)"
    Write-Info "  2. OpenRouter    API key, hundreds of models, pay-per-token"
    Write-Info "  3. Anthropic     Claude via API key or 'hermes auth add anthropic'"
    Write-Info "  4. OpenAI        API key"
    Write-Info "  5. Local         Ollama/vLLM/llama.cpp, no key, nothing leaves your machine"
    Write-Info "  6. Custom        any OpenAI-compatible endpoint"
    $choice = Read-Host "Choose [1-6, default 1]"
    if ($choice -eq '1' -or [string]::IsNullOrWhiteSpace($choice)) {
        if (-not $nousLogged) { Invoke-Native hermes @('setup', '--portal') }
        else { Write-Ok "Nous Portal already authenticated" }
    } else {
        Invoke-Native hermes @('setup', 'model')
    }
}

# --- 4. model ---------------------------------------------------------------
# YOLO does NOT pin a model here. model.default is chosen by the selector
# below (step 5), which probes the live free tier and only promotes a model
# after clean passes. -Model still overrides: it is written here and the
# selector then treats it as the incumbent (its gate will not demote it).

if ($Yolo) {
    Write-Step "Model selection"
    if ($Model) {
        Invoke-Native hermes @('config', 'set', 'model.default', $Model)
        Write-Ok "default model -> $Model (pinned by -Model)"
    } else {
        Write-Info "No -Model given: the live selector in step 5 picks model.default."
    }
} else {
    Write-Step "Model selection"
    $current = Get-CurrentDefault
    Write-Info "The live selector below probes the Nous free tier and ranks models"
    Write-Info "by release date (newest first) after each passes a real token budget."
    if (-not $Model) {
        $Model = Read-Host "Model id (blank keeps current: $current)"
    }
    if ($Model) {
        Invoke-Native hermes @('config', 'set', 'model.default', $Model)
        Write-Ok "default model -> $Model"
    }
}

# --- 4b. provider API keys (optional) -------------------------------------
# Offer to configure provider keys (NVIDIA/Google/Novita/Ollama/OpenRouter) so
# the selector can probe those providers. Keys are stored in Windows user env.
if (-not $SchedulerOnly -and -not $SelectorOnly) {
    $envScript = Join-Path $ScriptBaseDir 'Set-EnvKeys.ps1'
    if (-not (Test-Path -LiteralPath $envScript -PathType Leaf)) {
        Write-Warn "Set-EnvKeys.ps1 not found in $ScriptBaseDir; skipping provider keys"
        $envScript = $null
    }
    if ($envScript) {
        Write-Step "Provider API keys (optional)"
        Write-Info "Enables probing of NVIDIA / Google / Novita / Ollama Cloud / OpenRouter."
        $ans = Read-Host "Configure provider API keys now? [y/N]"
        if ($ans -eq 'y' -or $ans -eq 'Y') {
            # Dot-source so the keys land in this process env and are inherited
            # by the selector child process below.
            . $envScript
        } else {
            Write-Info "Skipped. Run Set-EnvKeys.ps1 later, then re-run the selector with --refresh."
        }
    }
}

} # end else (SelectorOnly)

# --- 5. model selector -----------------------------------------------------
# The selector probes the live free-tier models, refreshes
# fallback_providers + agent.reasoning_overrides through Hermes' own
# comment-preserving config writer, and maintains the promotion history
# (model.default only moves to a challenger after clean passes). It is the
# only source of the free chain -- no static list is kept as a backup.
$runSelector = $Yolo
if (-not $Yolo) {
    $sel = Read-Host "Run the live model selector? Probes free models over the network (~2 min) [Y/n]"
    $runSelector = ($sel -ne 'n')
}

if ($runSelector) {
    Write-Step "Model selector (live probe of the free tier)"
    # The venv lives inside the hermes install dir. Derive it from HERMES_HOME,
    # then fall back to the directory of the `hermes` on PATH -- HERMES_HOME is
    # only the data dir, and the two can legitimately differ (a custom HERMES_HOME
    # with a standard install, or vice versa).
    $pyCandidates = @(
        (Join-Path $HermesHome 'hermes-agent\venv\Scripts\python.exe')
    )
    $hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermesCmd -and $hermesCmd.Source) {
        $hermesDir = Split-Path $hermesCmd.Source -Parent
        # venv\Scripts\hermes -> venv\Scripts\python.exe (same dir on Windows)
        $pyCandidates += (Join-Path $hermesDir 'python.exe')
        # bin\hermes -> ..\hermes-agent\venv\Scripts\python.exe (sibling venv)
        $pyCandidates += (Join-Path (Split-Path $hermesDir -Parent) 'hermes-agent\venv\Scripts\python.exe')
        # bin\hermes -> ..\venv\Scripts\python.exe (venv next to bin)
        $pyCandidates += (Join-Path (Split-Path $hermesDir -Parent) 'venv\Scripts\python.exe')
    }
    # Last resort: python on PATH (may not have yaml, but we'll try)
    # Filter out the Windows Store stub (WindowsApps\python.exe) -- it's not a real Python.
    $pyOnPath = Get-Command python -ErrorAction SilentlyContinue
    if ($pyOnPath -and $pyOnPath.Source -and $pyOnPath.Source -notlike '*WindowsApps*') {
        $pyCandidates += $pyOnPath.Source
    }
    # Filter out WindowsApps stub and validate each candidate actually runs
    $py = $null
    $pyErrors = @()
    foreach ($candidate in $pyCandidates) {
        if ($candidate -like '*WindowsApps*') { continue }
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        # Test if this python actually works (not a Store stub)
        $testResult = & $candidate -c "print('ok')" 2>&1
        if ($LASTEXITCODE -eq 0 -and $testResult -match 'ok') {
            $py = $candidate
            break
        }
        $pyErrors += "    $candidate -> exit $LASTEXITCODE : $testResult"
    }
    if (-not $py) {
        $nl = [Environment]::NewLine
        $errMsg = "No working Python found. The selector cannot run without it."
        if ($pyErrors.Count -gt 0) {
            $errMsg += $nl + "What was tried:" + $nl + ($pyErrors -join $nl)
        }
        $errMsg += $nl + "Suggestions:"
        $errMsg += $nl + "  1. Run 'hermes doctor' to check your Hermes installation"
        $errMsg += $nl + "  2. Try reinstalling: hermes update --yes"
        $errMsg += $nl + "  3. If you have Python installed elsewhere, run the selector manually:"
        $errMsg += $nl + "     & 'C:\path\to\python.exe' select-models.py --hermes-home '$HermesHome'"
        $errMsg += $nl + "  4. Ask for help: https://github.com/H4ppyNZ/easy-hermes/issues"
        throw $errMsg
    }
    Write-Ok "selector python: $py"
    # Ensure PyYAML is available in the venv (the selector imports yaml)
    $yamlCheck = & $py -c "import yaml" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Installing pyyaml into the venv..."
        & $py -m pip install pyyaml 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install pyyaml. The selector cannot run without it."
        }
        Write-Ok "pyyaml installed"
    }
    $selectorPy = Get-SelectorScript
    # Belt and braces: the script reconfigures its own stdout at import,
    # but a caller without this env var used to crash it under cp1252.
    $env:PYTHONUTF8 = '1'
    $selectorArgs = @($selectorPy, '--hermes-home', $HermesHome, '--full')
    # Bootstrap: with NO incumbent in model.default the gate's 2-clean-pass
    # margin would decline to promote anything, leaving the default unset --
    # and the runtime then resolves an unset default to the provider's PAID
    # model (z-ai/glm-5.2, ~$0.9/1M) in silence. There is no incumbent to
    # protect on a virgin config, so one clean pass is the right bar; every
    # later nightly run goes back to the default margin of 2.
    if (-not (Get-CurrentDefault)) {
        $selectorArgs += @('--promotion-margin', '1')
        Write-Info "No model.default yet: promoting the newest model that passes cleanly tonight."
    }
    $rc = Invoke-Native $py @selectorArgs
    if ($rc -eq 0) {
        Write-Ok "selector complete: fallback chain + promotion history updated"
    } else {
        Write-Warn "selector exited $rc -- config was left as it was. Re-run it later:"
        Write-Info ("  & `"$py`" `"$selectorPy`" --hermes-home `"$HermesHome`"")
    }
}

# A run that finished with no model.default is not a harmless gap: the runtime
# silently substitutes the provider's PAID model (z-ai/glm-5.2, ~$0.9/1M).
# After the selector ran and still produced nothing, that is a failure -- stop.
# When the selector was declined (guided only), warn and let the operator fix
# it themselves rather than killing their interactive session.
if (-not (Get-CurrentDefault)) {
    if ($runSelector) {
        throw "model.default is still unset after the selector ran -- refusing to leave a config that would silently bill a paid model. Re-run the selector, or pin one with: hermes model"
    }
    Write-Warn "model.default is unset and the selector was declined. Hermes will silently fall back to the provider's paid model until you pick one: hermes model"
} else {
    Write-Ok "model.default -> $(Get-CurrentDefault)"
}

# --- 6. connectors ----------------------------------------------------------
# Hermes can connect to messaging platforms (Discord, WhatsApp, Telegram, Slack,
# etc.) so your agent is available where you already chat. This step encourages
# setting up at least one connector and walks through the process.
$connectors = @(
    @{ Name = "Discord"; Desc = "Bot for Discord servers" }
    @{ Name = "WhatsApp"; Desc = "Bot via WhatsApp (QR code scan)" }
    @{ Name = "Telegram"; Desc = "Bot for Telegram chats" }
    @{ Name = "Slack";    Desc = "Bot for Slack workspaces" }
)

Write-Step "Connectors (optional)"
Write-Info "Connect Hermes to messaging platforms so your agent is available"
Write-Info "where you already chat. Each connector needs its own app/bot setup."

if ($Yolo) {
    Write-Info "Yolo mode: skipping connector setup. Add later with 'hermes setup'."
} else {
    $setupConnector = Read-Host "`nSet up a messaging connector now? [y/N]"
    if ($setupConnector -eq 'y') {
        Write-Host "`nAvailable connectors:"
        for ($i = 0; $i -lt $connectors.Count; $i++) {
            Write-Host "  $($i+1). $($connectors[$i].Name) - $($connectors[$i].Desc)"
        }
        Write-Host "  0. Skip for now"
        $connChoice = Read-Host "Choose [0-$($connectors.Count), default 0]"
        
        if ($connChoice -ne '0' -and [int]$connChoice -ge 1 -and [int]$connChoice -le $connectors.Count) {
            $chosen = $connectors[[int]$connChoice - 1].Name
            Write-Step "Setting up $chosen connector"
            
            switch ($chosen) {
                "Discord" {
                    Write-Info "Discord bot setup:"
                    Write-Info "  1. Go to https://discord.com/developers/applications"
                    Write-Info "  2. Create a new application, then add a bot"
                    Write-Info "  3. Copy the bot token"
                    Write-Info "  4. Run: hermes setup"
                    Write-Info "  5. Follow the prompts to add Discord as a connector"
                    Write-Info "  6. Paste your bot token when prompted"
                    Write-Info "  7. Invite the bot to your server"
                }
                "WhatsApp" {
                    Write-Info "WhatsApp connector setup:"
                    Write-Info "  1. Run: hermes setup"
                    Write-Info "  2. Choose WhatsApp from the connector list"
                    Write-Info "  3. A QR code will appear -- scan it with WhatsApp"
                    Write-Info "  4. Your agent is now available on WhatsApp"
                }
                "Telegram" {
                    Write-Info "Telegram bot setup:"
                    Write-Info "  1. Message @BotFather on Telegram"
                    Write-Info "  2. Use /newbot and follow the prompts"
                    Write-Info "  3. Copy the bot token"
                    Write-Info "  4. Run: hermes setup"
                    Write-Info "  5. Follow the prompts to add Telegram as a connector"
                    Write-Info "  6. Paste your bot token when prompted"
                }
                "Slack" {
                    Write-Info "Slack app setup:"
                    Write-Info "  1. Go to https://api.slack.com/apps and create a new app"
                    Write-Info "  2. Add a bot user and copy the bot token"
                    Write-Info "  3. Run: hermes setup"
                    Write-Info "  4. Follow the prompts to add Slack as a connector"
                    Write-Info "  5. Paste your bot token when prompted"
                }
            }
            
            Write-Info "`nAfter completing the above, run 'hermes doctor' to verify the connector is working."
        }
    }
}

# --- 7. optional extras -----------------------------------------------------
if (-not $Yolo) {
    $acp = Read-Host "`nSet up ACP for your editor (VS Code/Zed/JetBrains)? [y/N]"
    if ($acp -eq 'y') {
        Write-Step "ACP editor integration"
        Invoke-Native hermes @('acp', '--check')
        Write-Info "VS Code    : install the 'ACP Client' extension, then pick Hermes Agent"
        Write-Info "Zed        : settings.json -> agent_servers -> {command: 'hermes', args: ['acp']}"
        Write-Info "JetBrains  : any ACP plugin pointed at 'hermes acp'"
        Write-Info "Note       : ACP needs the acp extra; run 'hermes pm install acp' if --check failed."
    }
}

# --- 8. verify --------------------------------------------------------------
Write-Step "Verify"
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { & hermes doctor 2>&1 | Select-Object -First 12 | Out-Host }
finally { $ErrorActionPreference = $prevEap }

# --- 8. primer --------------------------------------------------------------
if (-not $SkipDocs) {
    Write-Step "First-run primer"
    Write-Host @"
  WHAT YOU JUST GOT
  -----------------
  Hermes is a terminal-native AI agent. It thinks with an LLM (the model you
  picked) and it can ACT: read/write files, run terminal commands, browse
  the web, remember across sessions, and learn reusable procedures (skills).

  FIVE THINGS TO KNOW
  -------------------
  1. hermes                interactive chat. /help lists slash commands,
                           /model swaps models mid-session.
  2. hermes chat -q "..."  one-shot answer, no session.
  3. hermes model          change provider or model at any time.
  4. hermes doctor         health check: provider and tool status.
  5. /reload-mcp           reload MCP servers after config changes.

  LLM / AGENT / ACP / MCP - one line each
  ---------------------------------------
  LLM    the brain. Nous Portal (300+ models, one login), OpenRouter,
         Anthropic, OpenAI, Gemini, or a LOCAL model (Ollama) for full privacy.
  Agent  Hermes itself: an LLM wired to tools, memory and skills. It loops
         think -> call tool -> observe -> repeat, with approval prompts only
         for risky commands.
  ACP    Agent Client Protocol: an editor (VS Code, Zed, JetBrains) drives
         Hermes over stdio. Server command: 'hermes acp'.
  MCP    Model Context Protocol: external tool servers (GitHub, filesystem,
         databases, browsers) loaded as extra tools. Add: hermes mcp add NAME
         --command npx --args -y,@server/pkg. Test: hermes mcp test NAME.

  FREE / PRIVATE PATHS
  --------------------
  - Free tier: the fallback chain and model.default are both set by the live
    selector, so the whole free path is $0.00 on Nous.
  - The selector is re-runnable any time: it probes the free models, keeps
    a promotion history, and only moves model.default to a challenger after
    2 consecutive clean passes. Run it again from this installer with
    -SkipInstall, or directly:
      & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" `
        select-models.py --hermes-home "$env:LOCALAPPDATA\hermes"
  - Fully local, nothing leaves your machine: run Ollama, then 'hermes model'
    -> Custom endpoint -> http://localhost:11434/v1 -> your model. Match the
    context length to your Ollama num_ctx.

  DOCS
  ----
  https://hermes-agent.nousresearch.com/docs   (llms.txt indexes everything)

"@
}

# --- Scheduler registration (yolo) -----------------------------------------
if ($Yolo) {
    Write-Step "Registering scheduled tasks (yolo)"
    Register-ScheduledScripts
}

Write-Step "Done. Next: hermes   (or: hermes chat -q `"hello`")"
