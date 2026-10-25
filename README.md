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
| `& ([scriptblock]::Create((iwr -useb 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1?v=3').Content)) -Yolo` | Full unattended install + auth + selector |
| `& ([scriptblock]::Create((iwr -useb 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1?v=3').Content)) -Yolo -Model inclusionai/ling-3.1-flash` | Yolo with a pinned model |
| `& ([scriptblock]::Create((iwr -useb 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1?v=3').Content)) -SkipInstall` | Skip install (hermes already on PATH) |
| `& ([scriptblock]::Create((iwr -useb 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1?v=3').Content)) -SelectorOnly` | Just run the model selector |
| `& ([scriptblock]::Create((iwr -useb 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1?v=3').Content)) -SchedulerOnly` | Register the daily scheduled task |

## Execution policy

If you get `cannot be loaded because running scripts is disabled on this system`, your execution policy is set to `Restricted` (the Windows default). The scriptblock pattern is still subject to execution policy — it applies to all PowerShell execution, not just `.ps1` files on disk.

**Temporary bypass (recommended):** Open PowerShell and run:

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
```

This lasts only for the current window. Close the window and you're back to normal. Then run the one-liner in that same window.

**Persistent change:** If you want it to stick for your user account:

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser
```

**To revert** either change:

```powershell
Set-ExecutionPolicy -ExecutionPolicy Restricted -Scope Process      # revert process-scope
Set-ExecutionPolicy -ExecutionPolicy Undefined -Scope CurrentUser    # revert user-scope
```

`Restricted` is the Windows default; `Undefined` also resolves to it but clears your explicit override.

## Why `& ([scriptblock]::Create(...))` instead of `iwr | iex`?

`iwr | iex` executes the downloaded script but **cannot pass parameters** to it — `-Yolo` would go to `iex`, not the script. The scriptblock cast pattern downloads the content and invokes it as a scriptblock, so parameters bind correctly.


## NVIDIA NIM free-tier model polling

`poll_nvidia.py` lists the models exposed on NVIDIA NIM's OpenAI-compatible
endpoint (`GET https://integrate.api.nvidia.com/v1/models`). NIM has no
explicit free-SKU flag, so every returned model is treated as free-tier usable
subject to the free-tier rate limits (~40 RPM). Free-tier eligibility is
quota-based, not a listing attribute — treat output as best-effort/draft.

Auth (environment variables only):

```powershell
$env:NVIDIA_API_KEY = "<your key>"   # or NGC_API_KEY
python poll_nvidia.py
python poll_nvidia.py --json
```

Caveats: an NVIDIA account + API key is required (card may not be required);
rate limits ~40 RPM on the free tier; availability can be region-dependent;
geographic restrictions may apply.

## Files

- `hermes-setup.ps1` — main installer (install, auth, model selection, selector, primer)
- `hermes-model-selector.py` — live free-tier probe, promotion gate, fallback chain writer
- `hermes-ollama-selector.py` — probe local Ollama models and merge them into the fallback chain (see docs/ollama-selector.md)
- `poll_nvidia.py` — poll NVIDIA NIM free-tier models from integrate.api.nvidia.com (best-effort; free-tier eligibility is quota-based, not a listing flag)
- `Set-ModelSelectorTask.ps1` — registers a daily Windows scheduled task for the selector (4 AM)
- `NightlyRestartTask.ps1` — nightly maintenance: hermes update, selector, gateway restart, extra tasks
- `Set-NightlyTask.ps1` — registers a daily Windows scheduled task for the nightly restart (3 AM)
- `docs/installer-safety.md` — blast radius and safe re-run order for each step
- `tools/` — operational helper scripts (fallback config) with README
- `README.md` — this file
