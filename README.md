# easy-hermes

One-liner bootstrap for Hermes Agent with free-tier model selection.

## Quick start (recommended)

```powershell
& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -Yolo
```

This installs Hermes, authenticates (one browser prompt), runs the live model selector, and prints a first-run primer.

## All one-liners

| Command | What it does |
|---------|--------------|
| `& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -Yolo` | Full unattended install + auth + selector |
| `& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -Yolo -Model inclusionai/ling-3.1-flash` | Yolo with a pinned model |
| `& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -SkipInstall` | Skip install (hermes already on PATH) |
| `& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -SelectorOnly` | Just run the model selector |
| `& ([scriptblock]::Create((iwr -useb https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1).Content)) -SchedulerOnly` | Register the daily scheduled task |

## Execution policy

No special handling needed. The scriptblock pattern (`[scriptblock]::Create(...)`) downloads the script as a string and executes it in-memory — it never touches a `.ps1` file on disk, so execution policy doesn't apply. It works from PowerShell (any execution policy), cmd.exe, or Windows Terminal without modification.

## Why `& ([scriptblock]::Create(...))` instead of `iwr | iex`?

`iwr | iex` executes the downloaded script but **cannot pass parameters** to it — `-Yolo` would go to `iex`, not the script. The scriptblock cast pattern downloads the content and invokes it as a scriptblock, so parameters bind correctly.

## Files

- `hermes-setup.ps1` — main installer (install, auth, model selection, selector, primer)
- `hermes-model-selector.py` — live free-tier probe, promotion gate, fallback chain writer
- `Set-ModelSelectorTask.ps1` — registers a daily Windows scheduled task for the selector
