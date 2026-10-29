# Registers a Hermes cron job that monitors the NightlyRestartTask Windows
# Event Log (same pattern as the 7f9c2cdc026b job). Runs daily at 7:20 AM.
#
# Usage:
#   .\Set-HermesCronMonitor.ps1
#   .\Set-HermesCronMonitor.ps1 -Schedule '0 7 * * *' -Deliver 'discord:1234567890'
param(
    [string]$Schedule = '20 7 * * *',
    [string]$Name = 'NightlyRestart monitor',
    [string]$Deliver = 'local'
)

$ErrorActionPreference = 'Stop'

$hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
if (-not $hermesCmd) {
    throw "hermes not found on PATH. Install Hermes first."
}

$promptFile = Join-Path $PSScriptRoot 'cron\nightly-monitor-prompt.txt'
if (-not (Test-Path -LiteralPath $promptFile)) {
    throw "Prompt file not found: $promptFile"
}

$pythonExe = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = 'python' }

$existingCheck = & $pythonExe -c "import json,os; p=os.path.join(os.environ['LOCALAPPDATA'],'hermes','cron','jobs.json'); d=json.load(open(p)); print('EXISTS' if any(j.get('name')=='$Name' for j in d.get('jobs',[])) else 'NONE')" 2>&1 | Out-String
if ($existingCheck -match 'EXISTS') {
    Write-Output "Hermes cron job '$Name' already exists; skipping."
    exit 0
}

# Create with a placeholder prompt (CLI command-line length limits prevent
# passing the full 6KB prompt directly), then patch the prompt in jobs.json.
$createOut = & hermes cron create $Schedule "placeholder" --name $Name --deliver $Deliver 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "hermes cron create failed: $createOut"
}
$jobId = $null
if ($createOut -match 'Created job:\s*([0-9a-f]+)') {
    $jobId = $Matches[1]
}
if (-not $jobId) {
    throw "Could not parse job id from: $createOut"
}

$patchScript = @"
import json, os
path = os.path.join(os.environ['LOCALAPPDATA'], 'hermes', 'cron', 'jobs.json')
prompt_path = r'$promptFile'
with open(prompt_path, encoding='utf-8') as f:
    prompt = f.read()
with open(path, encoding='utf-8') as f:
    data = json.load(f)
for j in data.get('jobs', []):
    if j.get('id') == '$jobId':
        j['prompt'] = prompt
        break
tmp = path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
os.replace(tmp, path)
print('patched', '$jobId')
"@
$patchFile = Join-Path $env:TEMP 'patch_cron_prompt.py'
Set-Content -LiteralPath $patchFile -Value $patchScript -Encoding UTF8
& $pythonExe $patchFile
if ($LASTEXITCODE -ne 0) {
    throw "Failed to patch cron prompt."
}
Remove-Item -LiteralPath $patchFile -ErrorAction SilentlyContinue

Write-Output "Registered Hermes cron job '$Name' ($Schedule) deliver=$Deliver (id=$jobId)."