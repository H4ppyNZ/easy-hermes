# easy-hermes — Project Context

Windows-only wrapper: installer + free-model selector + scheduled tasks + nightly maintenance. Repo is the source of truth for the live system (scheduled tasks point at repo files), so a push is live immediately.

## Layout

Core: `hermes-setup.ps1` (installer), `select-models.py` (sole writer of model.default + fallback_providers), `hermes-ollama-selector.py` (local Ollama, opt-in), `providers.py` (registry), `release_dates.py`, `safe_url.py`, `atomic_io.py`, `chain_delta.py`.
Pollers: `pollers/poll_<provider>.py` + `poll_base.py` + `poll_template.py`.
PowerShell tasks: see table below. Docs: `docs/`. Tests: `tests/` (69, pure, no network).

## PowerShell scripts

| Script | Purpose | Key params |
|---|---|---|
| `hermes-setup.ps1` | Installer + selector entry point | `-Yolo -Model -SkipInstall -SkipDocs -SelectorOnly -SchedulerOnly` |
| `select-models.py` (py) | Live probe + promotion gate | `--dry-run --full --refresh --providers --set-default` |
| `Set-EnvKeys.ps1` | Writes provider keys to User env (secure prompt) | (none) |
| `Set-ModelSelectorTask.ps1` | Registers 4 AM selector task | `-RunAt -HermesHome` |
| `Set-NightlyTask.ps1` | Registers 3 AM nightly task | `-RunAt -HermesHome` |
| `Set-ReviewTask.ps1` | Registers 6 AM review task | `-RunAt -HermesHome` |
| `Set-HermesCronMonitor.ps1` | Registers 7:20 AM Hermes cron monitor | `-Schedule -Deliver` |
| `NightlyRestartTask.ps1` | nightly: update → selector → gateway restart → extras | `-SkipUpdate -DryRun -ExtraTasks` |
| `Run-ModelSelectorTask.ps1` | Generated wrapper that runs selector (PYTHONUTF8 set) | (auto) |
| `ReviewNightlyTask.ps1` | Reviews nightly run health/event log | `-HermesHome` |

All scheduled tasks live under `\Hermes\` task path (root `\` rejects spaces on this host) and run as Interactive principal (only fire when user logged on).

## Common commands

```powershell
python select-models.py --dry-run          # always first
python select-models.py --full
python -m pytest -q                       # use python/python3.11, not py
python -m py_compile select-models.py providers.py
[System.Management.Automation.Language.Parser]::ParseFile("hermes-setup.ps1", [ref]$null, [ref]$err)
git add -A; git commit -m "..."; git push origin main
```

## Conventions (non-obvious)

- ASCII-only `.ps1` (PS 5.1 cp1252 issue); `.ps1` needs CRLF; `param()` must be first statement; verify with PS 5.1 parser.
- Backup `config.yaml` before selector runs; selector rewrites it wholesale (atomic, `.yaml.bak`).
- Push after every change (scheduled tasks point at repo files); bump `?v=N` in README one-liners for fixes.
- After cutting a module: `grep -rn old-name` whole tree incl. comments/docs.
- Don't commit generated artifacts (`__pycache__/`, `out/`, `reports/`).

## Key paths

- Hermes home: `%LOCALAPPDATA%/hermes` (config.yaml, cache, skills, cron)
- Probe cache: `%LOCALAPPDATA%/hermes/cache/free_model_probe_cache.json`
- Task names under `\Hermes\`; interactive-principal tasks only fire when logged on.

## Model/provider gotchas

- Nous models like `reflection/beam-1.0-501b-a23b:free` aren't in `/model` picker; set via config or `/model`.
- Gemini keys start with `AQ.` and use `x-goog-api-key` header; OpenRouter needs `HTTP-Referer` + `X-Title`; NVIDIA models account-gated.
- Probe cache masks code changes; run `--refresh` after changing headers/validation.

## Deeper context

- Full encyclopedia: `easy-hermes` skill (auto-loads when working here).
- Safety/blast radius: `docs/installer-safety.md`. Capability tagging: `docs/capability-tags.md`.
- README: quick-start one-liners and selector flags.