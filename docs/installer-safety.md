# easy-hermes installer -- blast radius and safe re-run order

Repo: `C:\Users\mattw\Documents\repos\easy-hermes` (git, `github.com/H4ppyNZ/easy-hermes`).
Files: `hermes-setup.ps1` (installer; params `-Yolo`, `-Model`, `-SkipInstall`, `-SkipDocs`,
`-SelectorOnly`, `-SchedulerOnly`), `hermes-model-selector.py` (free-tier probe + promotion
gate), `Set-ModelSelectorTask.ps1` (registers a daily task). The README one-liners use the
scriptblock cast and pull `main`, so a push is live for testers at once.

## Re-running `hermes-setup.ps1` -- worst case per step

| Step | Runs when | What it can do |
|---|---|---|
| install | default (not `-SkipInstall`) | downloads and runs the official `install.ps1`; **upgrades an existing Hermes checkout** |
| auth | always | no-op when already authenticated (`hermes auth list` matches the provider) |
| model | `-Model` given | `hermes config set model.default` |
| selector | `-Yolo`, `-SelectorOnly`, or answered yes | **rewrites `fallback_providers` + `agent.reasoning_overrides`**; can move `model.default` |
| connectors / ACP | guided mode only | prompts; no writes |
| verify | always | `hermes doctor`, read-only |

**The install step is the real risk, and it is not data loss.** `install.ps1` treats an
existing `$InstallDir` containing `.git` as "update me": fetch, then
`git reset --hard origin/<branch>` -- or move the tree aside to
`$InstallDir.broken-<yyyyMMdd-HHmmss>` when the clone has no commits. So a plain re-run
silently upgrades Hermes to the latest stable and refreshes the dependency environment
under the user. What it preserves: uncommitted changes are `git stash push
--include-untracked`-ed before any reset, and commits absent from origin are parked in a
`refs/hermes-update-backups/...` rescue ref. `-SkipInstall` avoids the step entirely.

**The selector rewrites config wholesale.** `update_config()` REPLACES `fallback_providers`
and `agent.reasoning_overrides` with tonight's probe results -- it does not merge. If nothing
passes probing it returns early and leaves the config untouched. Writes go through ruamel's
`atomic_roundtrip_yaml_save`, so there is no half-written-config risk; the values churn, the
file never corrupts.

Promotion gate (`choose_default`): `PROMOTION_MARGIN = 2` consecutive clean passes,
hysteresis 7 days, `HISTORY_PRUNE_DAYS = 30`. The incumbent keeps the slot unless a
challenger is both eligible and strictly newer by the hysteresis window. An incumbent that
is in scope but FAILED this run's probe is the one case that triggers emergency promotion --
an incumbent that is merely absent from the free-model cache is "out of scope" and is never
displaced by that path.

Probe results vary run to run, so a re-run is not a no-op even when the gate keeps
`model.default`: the chain and the overrides still churn.

## Safe order if a re-run is wanted

1. Back up the config: `cp config.yaml config.yaml.bak`.
2. `--dry-run` the selector first -- it probes and prints the would-be config, writing
   nothing. Verify that claim: `config.yaml`, `cache/promotion_history.json` and
   `cache/fallback_model_cache.json` mtimes must be unchanged afterwards.
3. `-SkipInstall` to leave the Hermes version alone; `-SelectorOnly` when only the probe is wanted.
4. Report which keys would change and to what, rather than "it should be fine".

## Scope of the writes

The selector targets `$HERMES_HOME` (default `%LOCALAPPDATA%\hermes`), i.e. the **default
profile's** `config.yaml`. Other profiles under `profiles/<name>/` are untouched unless
`HERMES_HOME` is pointed at one.

## Verifying an edit before pushing

- `.ps1`: clean parse under the Windows PowerShell 5.1 parser --
  `[System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$err)`
  with `$err` empty. pwsh 7 passing proves nothing about `iex`.
- `.py`: `py_compile` with the Hermes venv python.
- Keep `.ps1` source ASCII-only: `write_file` emits UTF-8 without a BOM and 5.1 decodes
  that as CP1252, mangling any multi-byte character into a string-terminating quote.
