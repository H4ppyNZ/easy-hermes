"""Tests for NightlyRestartTask.ps1 extra tasks (step 4).

Extra tasks are host-specific, so they live in a gitignored extra-tasks.ps1 next
to the script rather than in the repo. These tests pin the contract: the config
file is gitignored, the docs describe it, and the loader/invoker behave.

The behavioural test extracts the real functions out of NightlyRestartTask.ps1
and runs them under Windows PowerShell 5.1, so it exercises the shipped code
rather than a copy. It needs no network and no Hermes install.
"""
import json
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "NightlyRestartTask.ps1")
GITIGNORE = os.path.join(REPO, ".gitignore")

PROBE = r"""
param([string]$Repo, [string]$Work)
$ErrorActionPreference = 'Stop'
$src = (Get-Content -Raw (Join-Path $Repo 'NightlyRestartTask.ps1')).Replace("`r`n", "`n")

function Get-Fn($name) {
    $s = $src.IndexOf("function $name {")
    if ($s -lt 0) { throw "function $name not found" }
    $e = $src.IndexOf("`n}`n", $s) + 3
    $src.Substring($s, $e - $s)
}
foreach ($fn in @('Get-ScriptblockTarget', 'Test-LiteralPathTarget',
                 'Get-ExtraTasksConfigPath', 'Resolve-ExtraTasks', 'Invoke-ExtraTasks')) {
    Invoke-Expression (Get-Fn $fn)
}

$script:msgs = @()
$script:HadError = $false
function Write-Log { param($Message, $EntryType = 'Information') $script:msgs += ([string]$Message) }
function Write-LogError { param($Message) $script:HadError = $true; $script:msgs += ([string]$Message) }

$out = [ordered]@{}
$bedrock = Join-Path $Work 'bedrockupdater.ps1'
Set-Content -Path $bedrock -Value 'Write-Output "bedrock ran"' -Encoding ASCII | Out-Null
$boom = Join-Path $Work 'boom.ps1'
Set-Content -Path $boom -Value 'throw "kaboom"' -Encoding ASCII | Out-Null

$out.target = [string](Get-ScriptblockTarget -Command ({ & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart }))
$out.varTarget = [string](Get-ScriptblockTarget -Command ({ & $bedrock -Restart }))
$out.literal = [bool](Test-LiteralPathTarget -Target $out.target)
$out.literalVar = [bool](Test-LiteralPathTarget -Target '$bedrock')

# config path: default is next to the script; an override wins; relative resolves
$out.defaultCfg = [string](Get-ExtraTasksConfigPath -ExtraTasksConfig '' -ScriptDir $Repo)
$out.overrideCfg = [string](Get-ExtraTasksConfigPath -ExtraTasksConfig (Join-Path $Work 'mine.ps1') -ScriptDir $Repo)
$out.relativeCfg = [string](Get-ExtraTasksConfigPath -ExtraTasksConfig 'mine.ps1' -ScriptDir $Repo)

$data = @(
    @{ Name = 'bedrock updater'; Command = { & $bedrock -Restart } },
    @{ Name = 'relative'; Path = 'bedrockupdater.ps1'; Arguments = @('-Restart') },
    @{ Name = 'off'; Enabled = $false; Command = { Write-Output 'never' } },
    @{ Name = 'boom'; Path = $boom }
)
$tasks = Resolve-ExtraTasks -Data $data -ConfigDir $Work
$out.count = @($tasks).Count
$out.names = @($tasks | ForEach-Object { $_.Name })
$rel = $tasks | Where-Object { $_.Name -eq 'relative' }
$out.relPath = [string]$rel.Path
$out.relArgs = @($rel.Arguments)
$out.relDir = [string]$rel.ConfigDir

$out.mapCount = @(Resolve-ExtraTasks -Data @{ 'first' = { Write-Output 'a' }; 'second' = { Write-Output 'b' } } -ConfigDir $Work).Count
$out.singleCount = @(Resolve-ExtraTasks -Data @{ Name = 'solo'; Command = { Write-Output 's' } } -ConfigDir $Work).Count
$out.paramCount = @(Resolve-ExtraTasks -Data $null -ConfigDir $Work -ExtraTasks @($bedrock, $boom)).Count

$run = @(
    @{ Name = 'ok'; Command = { & $bedrock -Restart } },
    @{ Name = 'missing'; Command = { & (Join-Path $Work 'nope.ps1') } },
    @{ Name = 'gone'; Path = (Join-Path $Work 'gone.ps1') },
    @{ Name = 'boom'; Path = $boom }
)
Invoke-ExtraTasks -Tasks $run
$out.hadError = [bool]$script:HadError
$out.messages = @($script:msgs)

$cfgBody = @'
$bedrock = 'BEDROCKPATH'
@(@{ Name = 'closure'; Command = { & $bedrock -Restart } })
'@
$cfgBody = $cfgBody.Replace('BEDROCKPATH', $bedrock)
$cfg = Join-Path $Work 'extra-tasks.ps1'
[System.IO.File]::WriteAllText($cfg, $cfgBody, (New-Object System.Text.ASCIIEncoding))
$loaded = . $cfg
$out.cfgCount = @(Resolve-ExtraTasks -Data $loaded -ConfigDir $Work).Count
$script:msgs = @()
Invoke-ExtraTasks -Tasks (Resolve-ExtraTasks -Data $loaded -ConfigDir $Work)
$out.cfgMessages = @($script:msgs)

# Regression: no config and no -ExtraTasks must yield ZERO tasks. A bare
# `return $tasks` on an empty array emits nothing, the caller captures $null,
# @($null).Count is 1, and step 4 "runs" a phantom task whose null Path makes
# `& $task.Path` throw -- failing the whole nightly run.
$empty = @(Resolve-ExtraTasks -Data $null -ConfigDir $Work -ExtraTasks @())
$out.emptyNull = ($null -eq $empty)
$out.emptyCount = @($empty).Count
$script:msgs = @()
$script:HadError = $false
Invoke-ExtraTasks -Tasks $empty
$out.emptyHadError = [bool]$script:HadError
$out.emptyMessages = @($script:msgs)

$out | ConvertTo-Json -Depth 6 -Compress
"""


def _run_probe(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    probe = tmp_path / "probe.ps1"
    probe.write_text(PROBE, encoding="ascii", newline="\r\n")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(probe), "-Repo", REPO, "-Work", str(work)],
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_extra_tasks_config_is_gitignored():
    """The host-specific config must never be committed."""
    text = open(GITIGNORE, encoding="utf-8").read()
    assert "extra-tasks.ps1" in text


def test_docs_describe_extra_tasks():
    doc = os.path.join(REPO, "docs", "nightly-restart.md")
    assert os.path.exists(doc), "docs/nightly-restart.md is missing"
    text = open(doc, encoding="utf-8").read()
    assert "extra-tasks.ps1" in text
    assert "bedrock updater" in text


def test_script_loads_config_next_to_itself():
    src = open(SCRIPT, encoding="utf-8").read()
    assert "extra-tasks.ps1" in src
    assert "Resolve-ExtraTasks" in src
    assert "Invoke-ExtraTasks" in src
    assert "[string]$ExtraTasksConfig" in src
    # the config is dot-sourced (so entry scriptblocks can close over its vars)
    assert "$configData = . $extraTasksPath" in src


def test_extra_task_functions_behave(tmp_path):
    out = _run_probe(tmp_path)

    # The user's exact example: the whole quoted path, not path + ' -Restart'.
    assert out["target"] == r"D:\Games\repoGaming\bedrockupdater.ps1"
    assert out["varTarget"] == ""
    assert out["literal"] is True
    assert out["literalVar"] is False

    # config path resolution
    assert out["defaultCfg"] == os.path.join(REPO, "extra-tasks.ps1")
    assert out["overrideCfg"] == str(tmp_path / "work" / "mine.ps1")
    assert os.path.isabs(out["relativeCfg"])
    assert out["relativeCfg"].endswith("mine.ps1")

    # Array form: disabled entry dropped, order preserved, Path/Arguments kept.
    assert out["count"] == 3
    assert out["names"] == ["bedrock updater", "relative", "boom"]
    assert out["relPath"] == "bedrockupdater.ps1"
    assert out["relArgs"] == ["-Restart"]
    assert out["relDir"] == str(tmp_path / "work")

    # Map and single-entry forms.
    assert out["mapCount"] == 2
    assert out["singleCount"] == 1

    # -ExtraTasks paths are appended.
    assert out["paramCount"] == 2

    # Invocation: real run captured, missing literal skipped, throw -> error,
    # and the run did not abort (all four tasks were attempted).
    assert out["hadError"] is True
    joined = "\n".join(out["messages"])
    assert "bedrock ran" in joined
    assert "SKIPPED (script not found:" in joined
    assert "kaboom" in joined
    assert joined.count("Invoking task") == 3  # ok, missing, boom; 'gone' skipped

    # A dot-sourced config file: the entry sees the file's own $bedrock.
    assert out["cfgCount"] == 1
    # The no-config case: zero tasks, no phantom entry, no error.
    assert out["emptyNull"] is False
    assert out["emptyCount"] == 0
    assert out["emptyHadError"] is False
    _nl = chr(10)
    assert "Running 0 additional task(s)" in _nl.join(out["emptyMessages"])
    assert "failed" not in _nl.join(out["emptyMessages"])
    cfg_joined = "\n".join(out["cfgMessages"])
    assert "closure" in cfg_joined
    assert "bedrock ran" in cfg_joined
