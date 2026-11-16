# Nightly restart task

`NightlyRestartTask.ps1` is the nightly maintenance job. It is registered by
`Set-NightlyTask.ps1` as the Windows task `Nightly Restart` under `\Hermes\`
(3:00 AM, Interactive principal, so it only fires while you are logged on).

Everything it does is written to the Windows Event Log under the source
`NightlyRestartTask`; the Hermes cron job "NightlyRestart monitor" reads that log
each morning and reports the outcome.

## The four steps

| # | Step | Notes |
|---|---|---|
| 1 | `hermes update --no-gateway-restart --yes` | skip with `-SkipUpdate` |
| 2 | `select-models.py --hermes-home <home> --full` | probes free models, applies the promotion gate, rewrites `model.default` + `fallback_providers`. Add `--dry-run` with `-DryRun` |
| 3 | `hermes gateway restart` | runs **after** step 2 so the new default takes effect. Skipped under `-DryRun` |
| 4 | Extra tasks | from `extra-tasks.ps1` and/or `-ExtraTasks` (this document) |

Step 2 before step 3 is deliberate: restarting the gateway first would leave the
old model in use until the following night.

## Extra tasks (step 4)

Extra tasks are host-specific, so they are **not** in the repo. They come from a
config file that lives next to `NightlyRestartTask.ps1`:

| Install shape | Config path |
|---|---|
| clone | `<repo>\extra-tasks.ps1` |
| one-liner | `%LOCALAPPDATA%\hermes\easy-hermes\extra-tasks.ps1` |

`extra-tasks.ps1` is listed in `.gitignore` and is **not** in `MANIFEST.json`, so
it is never overwritten by an install or an update, and it is never pushed. Copy
the example below, edit it, and it takes effect on the next run -- no need to
re-register the scheduled task.

`-ExtraTasksConfig <path>` points the script at a different file.

### The file is dot-sourced

`extra-tasks.ps1` is dot-sourced at script scope, so an entry's scriptblock can
see variables the file defines. This lets one path be reused by several entries:

```powershell
$bedrock = 'D:\Games\repoGaming\bedrockupdater.ps1'

@(
    @{ Name = 'bedrock updater'; Command = { & $bedrock -Restart } }
)
```

Keep the file to data and scriptblocks. It executes as the scheduled-task user
with that user's rights -- anything the file can do, the nightly run can do.

### Accepted shapes

The file must output exactly one of these.

**1. Array of entries** (ordered; the form to use when order matters):

```powershell
@(
    @{ Name = 'bedrock updater'; Command = { & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart } },
    @{ Name = 'log cleanup';    Path   = 'D:\scripts\cleanup.ps1'; Arguments = @('-KeepDays', 7) },
    @{ Name = 'optional thing'; Command = { & 'D:\scripts\thing.ps1' }; Enabled = $false }
)
```

**2. Name -> entry map** (unordered; shorter when you only need scriptblocks):

```powershell
@{
    'bedrock updater' = { & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart }
    'log cleanup'     = { & 'D:\scripts\cleanup.ps1' -KeepDays 7 }
}
```

**3. A single entry** (no array needed):

```powershell
@{ Name = 'bedrock updater'; Command = { & 'D:\Games\repoGaming\bedrockupdater.ps1' -Restart } }
```

### Entry keys

| Key | Required | Meaning |
|---|---|---|
| `Name` | no | Log label. Defaults to the script's file name, or `extra task` |
| `Command` | yes, unless `Path` | A scriptblock. Invoked as `& $Command`; may do anything, not just run a script |
| `Path` | yes, unless `Command` | Script/executable to run. Relative paths resolve against the config file's folder |
| `Arguments` | no | String array passed to `Path` |
| `Enabled` | no | `$false` skips the entry (with a log line) so you can keep it without deleting it |

`Command` wins when both `Command` and `Path` are present.

A hashtable that contains `Command` or `Path` is one entry; any other hashtable
is read as a name -> entry map.

### Adding a task without a config file

Pass paths on the command line. They are appended after the config file's tasks:

```powershell
.\NightlyRestartTask.ps1 -ExtraTasks 'D:\Games\repoGaming\bedrockupdater.ps1'
```

This is what `hermes-setup.ps1` / `Set-NightlyTask.ps1` cannot do for you: the
registered task passes no extras, so the config file is the way to make them
recur.

## Behaviour

- **A task whose literal target is missing is skipped**, with a log line, and the
  run continues. "Literal" means a quoted or drive-letter path, e.g.
  `& 'D:\scripts\x.ps1'`. A target built from a variable or expression
  (`& $tool`, `& (Join-Path $dir 'x.ps1')`) cannot be checked in advance, so it
  runs and a real failure is reported.
- **A task that throws is logged as an error** and the run continues with the
  next task. The script exits `1` at the end, which is what the monitor reports.
- Every task's output is captured and written to the event log (clamped to 30000
  characters, because `Write-EventLog` rejects anything longer).
- A config file that cannot be parsed is logged as an error; it does not abort
  the nightly run.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No extra tasks config at ...` | No `extra-tasks.ps1` next to the script, and no `-ExtraTasks`. Normal -- step 4 runs zero tasks |
| `SKIPPED (script not found: ...)` | A literal path in the config does not exist. Fix the path |
| `failed: The term '...' is not recognized` | The task's target was a variable/expression that did not resolve to a real script |
| `Extra tasks config failed to load` | Syntax error in `extra-tasks.ps1`. Run it in a PowerShell window to see the parse error |
| Task ran manually but not at night | The task is Interactive-principal: it only fires while you are logged on. Check `Get-ScheduledTask -TaskName 'Nightly Restart' -TaskPath '\Hermes\'` |

## Safety

- The file is gitignored on purpose: it holds local paths. Never commit it.
- It is not part of `MANIFEST.json`, so an install or auto-update never creates,
  overwrites, or deletes it.
- It runs with your user's rights at 3 AM. A task that deletes files, mounts
  shares, or posts to the network does so as you.
- `Set-NightlyTask.ps1` does not need to be re-run when the file changes: the
  task points at `NightlyRestartTask.ps1`, which reads the config on every run.
