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

## Pollers

Small, standalone scripts that list (not probe) provider model catalogs. They do
not rewrite `config.yaml`.

### OpenRouter (`poll_openrouter.py`) — verified (public API)
- Endpoint: `GET https://openrouter.ai/api/v1/models`
- Auth env vars: `OPENROUTER_API_KEY` (optional)
- Run: `python poll_openrouter.py` / `python poll_openrouter.py --json`
- Filter: `pricing.prompt == 0` AND `pricing.completion == 0`, or id ends with `:free`.

### NVIDIA NIM (`poll_nvidia.py`) — best-effort
- Endpoint: `GET https://integrate.api.nvidia.com/v1/models`
- Auth env vars: `NVIDIA_API_KEY` (or `NGC_API_KEY`) (required)
- Run: `python poll_nvidia.py` / `python poll_nvidia.py --json`
- Filter: all returned models (free-tier is quota-based, not a listing flag).

## Files

- `hermes-setup.ps1` — main installer (install, auth, model selection, selector, primer)
- `hermes-model-selector.py` — live free-tier probe, promotion gate, fallback chain writer
- `hermes-ollama-selector.py` — probe local Ollama models and merge them into the fallback chain (see docs/ollama-selector.md)
- `poll_openrouter.py` — poll OpenRouter's free models (see Pollers)
- `poll_nvidia.py` — poll NVIDIA NIM free-tier models (best-effort)
- `poll_gemini.py` — poll Google AI Studio (Gemini) free-tier models (best-effort / unverified)
- `poll_ollama_cloud.py` — poll Ollama Cloud model catalog (best-effort / unverified)
- `Set-ModelSelectorTask.ps1` — registers a daily Windows scheduled task for the selector (4 AM)
- `NightlyRestartTask.ps1` — nightly maintenance: hermes update, selector, gateway restart, extra tasks
- `Set-NightlyTask.ps1` — registers a daily Windows scheduled task for the nightly restart (3 AM)
- `docs/installer-safety.md` — blast radius and safe re-run order for each step
- `tools/` — operational helper scripts (fallback config) with README
- `README.md` — this file


## Gemini (Google AI Studio) polling (best-effort / unverified)

`poll_gemini.py` lists the models exposed by Google AI Studio
(`https://generativelanguage.googleapis.com/v1beta/models`).

Caveats (read before relying on the output):

- **Best-effort / unverified.** Google does not mark a free tier in the model
  listing; free-tier eligibility is quota-based and follows AI Studio documented
  free models and quotas. This script includes models supporting
  `generateContent` (chat/completion) and marks them best-effort/unverified.
  Confirm access with your own account.
- **Auth required.** Set `GOOGLE_API_KEY` (preferred) or `GEMINI_API_KEY`.
  The key is sent as `?key=` (and `x-goog-api-key` header).
- **Pagination.** Uses `pageToken`/`nextPageToken`.
- **May include non-free models.** The listing is not a free-tier catalog;
  verify quotas in AI Studio.
- **Terms of service.** Use in accordance with Google's ToS; do not hammer the
  endpoint (the script retries with backoff).

Auth env vars: `GOOGLE_API_KEY` (preferred) or `GEMINI_API_KEY`.
Output: normalized list with `id`, `display_name`, `context_window`,
`rate_limits` (null), `notes`.
## Ollama Cloud polling (best-effort / unverified)

`poll_ollama_cloud.py` lists the models exposed by Ollama Cloud
(`https://ollama.com/api/tags`, `https://api.ollama.com/v1/models`, ...).

Caveats (read before relying on the output):

- **Best-effort / unverified.** Ollama Cloud does not publish a pricing or
  free-tier field in its listing API, and "free" is governed by account quotas
  and which models your account can invoke. This script treats every listed
  model as free-tier subject to quotas unless an explicit `price`/`pricing`
  field is present (then only `price == 0` is kept). Confirm access with your
  own account.
- **Endpoint unverified.** The listing endpoints are not a stable, documented
  contract. The script tries several candidate URLs in order and uses the first
  that yields a parseable list. It may break without notice.
- **May require an account.** Some endpoints/models need `OLLAMA_API_KEY`
  (or `OLLAMA_CLOUD_API_KEY`). The public `https://ollama.com/api/tags`
  endpoint works without a key.
- **Geographic / availability limits.** Model availability can vary by region
  and account tier.
- **Terms of service.** Use in accordance with Ollama's ToS; do not hammer
  the endpoint (the script retries with backoff).

Auth env vars: `OLLAMA_API_KEY` (preferred) or `OLLAMA_CLOUD_API_KEY`.
Output: `out/ollama-cloud.json`.

